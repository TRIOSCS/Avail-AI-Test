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

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import require_buyer, require_user
from ..models import (
    Company,
    CustomerSite,
    InventorySnapshot,
    Offer,
    Quote,
    Requirement,
    Requisition,
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
    CustomerImportRow,
    EnrichDomainRequest,
    OfferCreate,
    OfferUpdate,
    QuoteCreate,
    QuoteReopen,
    QuoteResult,
    QuoteUpdate,
    SiteCreate,
    SiteUpdate,
)

router = APIRouter()


# ── Helpers ──────────────────────────────────────────────────────────────


def next_quote_number(db: Session) -> str:
    """Generate next sequential quote number: Q-YYYY-NNNN."""
    year = datetime.now(timezone.utc).year
    prefix = f"Q-{year}-"
    last = db.query(Quote).filter(
        Quote.quote_number.like(f"{prefix}%")
    ).order_by(Quote.id.desc()).first()
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
    quotes = db.query(Quote).filter(
        Quote.status.in_(["sent", "won", "lost"]),
    ).order_by(Quote.sent_at.desc().nullslast(), Quote.created_at.desc()).limit(100).all()
    mpn_upper = mpn.upper().strip()
    for q in quotes:
        for item in (q.line_items or []):
            if (item.get("mpn") or "").upper().strip() == mpn_upper:
                return {
                    "sell_price": item.get("sell_price"),
                    "margin_pct": item.get("margin_pct"),
                    "quote_number": q.quote_number,
                    "date": (q.sent_at or q.created_at).isoformat() if (q.sent_at or q.created_at) else None,
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
            if q.customer_site and q.customer_site.company else ""
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
    search: str = "", owner_id: int = 0,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    query = db.query(Company).filter(Company.is_active == True)  # noqa: E712
    if search.strip():
        safe = search.strip().replace("%", r"\%").replace("_", r"\_")
        query = query.filter(Company.name.ilike(f"%{safe}%"))
    companies = query.order_by(Company.name).all()

    result = []
    for c in companies:
        sites = []
        for s in c.sites:
            if not s.is_active:
                continue
            if owner_id and s.owner_id != owner_id:
                continue
            open_count = db.query(sqlfunc.count(Requisition.id)).filter(
                Requisition.customer_site_id == s.id,
                Requisition.status.notin_(["archived", "won", "lost"]),
            ).scalar() or 0
            sites.append({
                "id": s.id, "site_name": s.site_name,
                "owner_id": s.owner_id,
                "owner_name": s.owner.name if s.owner else None,
                "contact_name": s.contact_name, "contact_email": s.contact_email,
                "contact_phone": s.contact_phone, "contact_title": s.contact_title,
                "payment_terms": s.payment_terms, "shipping_terms": s.shipping_terms,
                "city": s.city, "state": s.state,
                "open_reqs": open_count, "notes": s.notes,
            })
        if owner_id and not sites:
            continue
        result.append({
            "id": c.id, "name": c.name, "website": c.website,
            "industry": c.industry, "notes": c.notes,
            "domain": c.domain, "linkedin_url": c.linkedin_url,
            "legal_name": c.legal_name, "employee_size": c.employee_size,
            "hq_city": c.hq_city, "hq_state": c.hq_state, "hq_country": c.hq_country,
            "last_enriched_at": c.last_enriched_at.isoformat() if c.last_enriched_at else None,
            "enrichment_source": c.enrichment_source,
            "site_count": len(sites), "sites": sites,
        })
    return result


@router.post("/api/companies")
async def create_company(payload: CompanyCreate, user: User = Depends(require_user), db: Session = Depends(get_db)):
    company = Company(
        name=payload.name, website=payload.website,
        industry=payload.industry, notes=payload.notes,
        domain=payload.domain, linkedin_url=payload.linkedin_url,
    )
    db.add(company)
    db.commit()
    return {"id": company.id, "name": company.name}


@router.put("/api/companies/{company_id}")
async def update_company(
    company_id: int, payload: CompanyUpdate,
    user: User = Depends(require_user), db: Session = Depends(get_db),
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
    company_id: int, payload: SiteCreate,
    user: User = Depends(require_user), db: Session = Depends(get_db),
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
    site_id: int, payload: SiteUpdate,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(site, field, value)
    db.commit()
    return {"ok": True}


@router.get("/api/sites/{site_id}")
async def get_site(site_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404)
    reqs = db.query(Requisition).filter(
        Requisition.customer_site_id == site_id,
    ).order_by(Requisition.created_at.desc()).limit(20).all()
    return {
        "id": site.id, "company_id": site.company_id,
        "company_name": site.company.name if site.company else None,
        "company_domain": site.company.domain if site.company else None,
        "company_website": site.company.website if site.company else None,
        "site_name": site.site_name,
        "owner_id": site.owner_id,
        "owner_name": site.owner.name if site.owner else None,
        "contact_name": site.contact_name, "contact_email": site.contact_email,
        "contact_phone": site.contact_phone, "contact_title": site.contact_title,
        "contact_linkedin": site.contact_linkedin,
        "address_line1": site.address_line1, "address_line2": site.address_line2,
        "city": site.city, "state": site.state, "zip": site.zip, "country": site.country,
        "payment_terms": site.payment_terms, "shipping_terms": site.shipping_terms,
        "notes": site.notes,
        "recent_reqs": [{
            "id": r.id, "name": r.name, "status": r.status,
            "requirement_count": len(r.requirements),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in reqs],
    }


# ── Enrichment (shared for vendors + customers) ─────────────────────────


@router.post("/api/enrich/company/{company_id}")
async def enrich_company(
    company_id: int, payload: EnrichDomainRequest = EnrichDomainRequest(),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Enrich a customer company with external data."""
    if not settings.clay_api_key and not settings.explorium_api_key:
        raise HTTPException(503, "No enrichment providers configured — set CLAY_API_KEY or EXPLORIUM_API_KEY in .env")
    from ..enrichment_service import apply_enrichment_to_company, enrich_entity
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
    db.commit()
    return {"ok": True, "updated_fields": updated, "enrichment": enrichment}


@router.post("/api/enrich/vendor/{card_id}")
async def enrich_vendor_card(
    card_id: int, payload: EnrichDomainRequest = EnrichDomainRequest(),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Enrich a vendor card with external data."""
    if not settings.clay_api_key and not settings.explorium_api_key:
        raise HTTPException(503, "No enrichment providers configured — set CLAY_API_KEY or EXPLORIUM_API_KEY in .env")
    from ..enrichment_service import apply_enrichment_to_vendor, enrich_entity
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
    domain: str = "", name: str = "", title: str = "",
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Find suggested contacts at a company from enrichment providers."""
    if not settings.clay_api_key and not settings.explorium_api_key:
        raise HTTPException(503, "No enrichment providers configured — set CLAY_API_KEY or EXPLORIUM_API_KEY in .env")
    from ..enrichment_service import find_suggested_contacts
    if not domain:
        raise HTTPException(400, "domain parameter is required")
    domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    contacts = await find_suggested_contacts(domain, name, title)
    return {"domain": domain, "contacts": contacts, "count": len(contacts)}


@router.post("/api/suggested-contacts/add-to-vendor")
async def add_suggested_to_vendor(
    payload: AddContactsToVendor,
    user: User = Depends(require_buyer), db: Session = Depends(get_db),
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
    user: User = Depends(require_user), db: Session = Depends(get_db),
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
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Run schema discovery against Acctivate SQL Server."""
    if user.email.lower() not in settings.admin_emails:
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
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Manually trigger an Acctivate sync run."""
    if user.email.lower() not in settings.admin_emails:
        raise HTTPException(403, "Admin only")
    if not settings.acctivate_host:
        raise HTTPException(400, "ACCTIVATE_HOST not configured")
    from ..acctivate_sync import run_sync
    result = run_sync(db)
    return result


@router.get("/api/admin/sync-logs")
async def get_sync_logs(
    source: str = "", limit: int = 20,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """View recent sync log entries."""
    if user.email.lower() not in settings.admin_emails:
        raise HTTPException(403, "Admin only")
    q = db.query(SyncLog).order_by(SyncLog.started_at.desc())
    if source:
        q = q.filter(SyncLog.source == source)
    logs = q.limit(limit).all()
    return [
        {
            "id": l.id, "source": l.source, "status": l.status,
            "started_at": l.started_at.isoformat() if l.started_at else None,
            "finished_at": l.finished_at.isoformat() if l.finished_at else None,
            "duration_seconds": l.duration_seconds,
            "row_counts": l.row_counts, "errors": l.errors,
        }
        for l in logs
    ]


@router.get("/api/inventory")
async def get_inventory(
    mpn: str = "", user: User = Depends(require_user), db: Session = Depends(get_db),
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
async def list_users_simple(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Simple user list for owner dropdowns."""
    users = db.query(User).order_by(User.name).all()
    return [
        {"id": u.id, "name": u.name or u.email.split("@")[0], "email": u.email, "role": u.role}
        for u in users
    ]


# ── Customer Import ──────────────────────────────────────────────────────


@router.post("/api/customers/import")
async def import_customers(
    request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
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
                errors.append(f"Row {i+1}: missing company_name")
                continue
            company = db.query(Company).filter(
                sqlfunc.lower(Company.name) == company_name.lower(),
            ).first()
            if not company:
                company = Company(name=company_name)
                db.add(company)
                db.flush()
                created_companies += 1
            site = db.query(CustomerSite).filter(
                CustomerSite.company_id == company.id,
                sqlfunc.lower(CustomerSite.site_name) == site_name.lower(),
            ).first()
            if not site:
                site = CustomerSite(company_id=company.id, site_name=site_name)
                db.add(site)
                created_sites += 1
            owner_email = (row.get("owner_email") or "").strip().lower()
            if owner_email and owner_email in users_map:
                site.owner_id = users_map[owner_email].id
            for field in [
                "contact_name", "contact_email", "contact_phone", "contact_title",
                "payment_terms", "shipping_terms", "city", "state", "zip", "country", "notes",
            ]:
                val = row.get(field)
                if val:
                    setattr(site, field, val.strip() if isinstance(val, str) else val)
            addr = row.get("address")
            if addr:
                site.address_line1 = addr.strip()
        except Exception as e:
            errors.append(f"Row {i+1}: {str(e)}")
    db.commit()
    return {"created_companies": created_companies, "created_sites": created_sites, "errors": errors[:20]}


# ── Offers ───────────────────────────────────────────────────────────────


@router.get("/api/requisitions/{req_id}/offers")
async def list_offers(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from ..dependencies import get_req_for_user
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    offers = db.query(Offer).filter(Offer.requisition_id == req_id).order_by(
        Offer.requirement_id, Offer.unit_price,
    ).all()
    groups: dict[int, list] = {}
    for o in offers:
        key = o.requirement_id or 0
        if key not in groups:
            groups[key] = []
        groups[key].append({
            "id": o.id, "requirement_id": o.requirement_id,
            "vendor_name": o.vendor_name, "vendor_card_id": o.vendor_card_id,
            "mpn": o.mpn, "manufacturer": o.manufacturer,
            "qty_available": o.qty_available,
            "unit_price": float(o.unit_price) if o.unit_price else None,
            "lead_time": o.lead_time, "date_code": o.date_code,
            "condition": o.condition, "packaging": o.packaging,
            "moq": o.moq, "source": o.source, "status": o.status,
            "notes": o.notes,
            "entered_by": o.entered_by.name if o.entered_by else None,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        })
    result = []
    for r in req.requirements:
        target = float(r.target_price) if r.target_price else None
        last_q = get_last_quoted_price(r.primary_mpn, db)
        result.append({
            "requirement_id": r.id, "mpn": r.primary_mpn,
            "target_qty": r.target_qty, "target_price": target,
            "last_quoted": last_q, "offers": groups.get(r.id, []),
        })
    return result


@router.post("/api/requisitions/{req_id}/offers")
async def create_offer(
    req_id: int, payload: OfferCreate,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    from ..dependencies import get_req_for_user
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    norm_name = normalize_vendor_name(payload.vendor_name)
    card = db.query(VendorCard).filter(VendorCard.normalized_name == norm_name).first()
    offer = Offer(
        requisition_id=req_id, requirement_id=payload.requirement_id,
        vendor_card_id=card.id if card else None,
        vendor_name=card.display_name if card else payload.vendor_name,
        mpn=payload.mpn, manufacturer=payload.manufacturer,
        qty_available=payload.qty_available, unit_price=payload.unit_price,
        lead_time=payload.lead_time, date_code=payload.date_code,
        condition=payload.condition, packaging=payload.packaging,
        moq=payload.moq, source=payload.source,
        vendor_response_id=payload.vendor_response_id,
        entered_by_id=user.id, notes=payload.notes,
        status=payload.status,
    )
    db.add(offer)
    if req.status in ("active", "sourcing"):
        req.status = "quoting"
    db.commit()
    return {"id": offer.id, "vendor_name": offer.vendor_name, "mpn": offer.mpn}


@router.put("/api/offers/{offer_id}")
async def update_offer(
    offer_id: int, payload: OfferUpdate,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(offer, field, value)
    db.commit()
    return {"ok": True}


@router.delete("/api/offers/{offer_id}")
async def delete_offer(offer_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404)
    db.delete(offer)
    db.commit()
    return {"ok": True}


# ── Quotes ───────────────────────────────────────────────────────────────


@router.get("/api/requisitions/{req_id}/quote")
async def get_quote(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from ..dependencies import get_req_for_user
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    quote = db.query(Quote).filter(
        Quote.requisition_id == req_id,
    ).order_by(Quote.revision.desc()).first()
    if not quote:
        return None
    return quote_to_dict(quote)


@router.post("/api/requisitions/{req_id}/quote")
async def create_quote(
    req_id: int, payload: QuoteCreate,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    from ..dependencies import get_req_for_user
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    if not req.customer_site_id:
        raise HTTPException(400, "Requisition must be linked to a customer site before quoting")
    offer_ids = payload.offer_ids
    line_items = payload.line_items
    if offer_ids and not line_items:
        offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
        line_items = []
        for o in offers:
            target = None
            last_q_price = None
            if o.requirement:
                target = float(o.requirement.target_price) if o.requirement.target_price else None
                lq = get_last_quoted_price(o.mpn, db)
                last_q_price = lq.get("sell_price") if lq else None
            cost = float(o.unit_price) if o.unit_price else 0
            line_items.append({
                "mpn": o.mpn, "manufacturer": o.manufacturer,
                "qty": o.qty_available or 0, "cost_price": cost,
                "sell_price": cost, "margin_pct": 0,
                "lead_time": o.lead_time, "condition": o.condition,
                "offer_id": o.id, "target_price": target,
                "last_quoted_price": last_q_price,
            })
    site = db.get(CustomerSite, req.customer_site_id)
    quote = Quote(
        requisition_id=req_id, customer_site_id=req.customer_site_id,
        quote_number=next_quote_number(db), line_items=line_items,
        payment_terms=site.payment_terms if site else None,
        shipping_terms=site.shipping_terms if site else None,
        created_by_id=user.id,
    )
    db.add(quote)
    db.commit()
    return quote_to_dict(quote)


@router.put("/api/quotes/{quote_id}")
async def update_quote(
    quote_id: int, payload: QuoteUpdate,
    user: User = Depends(require_user), db: Session = Depends(get_db),
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
        quote.total_margin_pct = round((total_sell - total_cost) / total_sell * 100, 2) if total_sell > 0 else 0
    for field, value in updates.items():
        setattr(quote, field, value)
    db.commit()
    return quote_to_dict(quote)


@router.post("/api/quotes/{quote_id}/send")
async def send_quote(quote_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404)
    quote.status = "sent"
    quote.sent_at = datetime.now(timezone.utc)
    req = db.get(Requisition, quote.requisition_id)
    if req and req.status not in ("won", "lost", "archived"):
        req.status = "quoted"
    db.commit()
    return {"ok": True, "status": "sent"}


@router.post("/api/quotes/{quote_id}/result")
async def quote_result(
    quote_id: int, payload: QuoteResult,
    user: User = Depends(require_user), db: Session = Depends(get_db),
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
    return {"ok": True, "status": payload.result}


@router.post("/api/quotes/{quote_id}/revise")
async def revise_quote(quote_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    old = db.get(Quote, quote_id)
    if not old:
        raise HTTPException(404)
    old.status = "revised"
    old_number = old.quote_number
    old.quote_number = f"{old_number}-R{old.revision}"
    new_quote = Quote(
        requisition_id=old.requisition_id, customer_site_id=old.customer_site_id,
        quote_number=old_number, revision=old.revision + 1,
        line_items=old.line_items, payment_terms=old.payment_terms,
        shipping_terms=old.shipping_terms, validity_days=old.validity_days,
        notes=old.notes, created_by_id=user.id,
    )
    db.add(new_quote)
    db.commit()
    return quote_to_dict(new_quote)


@router.post("/api/quotes/{quote_id}/reopen")
async def reopen_quote(
    quote_id: int, payload: QuoteReopen,
    user: User = Depends(require_user), db: Session = Depends(get_db),
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
            requisition_id=quote.requisition_id, customer_site_id=quote.customer_site_id,
            quote_number=old_number, revision=quote.revision + 1,
            line_items=quote.line_items, payment_terms=quote.payment_terms,
            shipping_terms=quote.shipping_terms, validity_days=quote.validity_days,
            notes=quote.notes, created_by_id=user.id,
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


# ── Pricing History ──────────────────────────────────────────────────────


@router.get("/api/pricing-history/{mpn}")
async def pricing_history(mpn: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    mpn_upper = mpn.upper().strip()
    quotes = db.query(Quote).filter(
        Quote.status.in_(["sent", "won", "lost"]),
    ).order_by(Quote.sent_at.desc().nullslast(), Quote.created_at.desc()).limit(500).all()
    history = []
    for q in quotes:
        for item in (q.line_items or []):
            if (item.get("mpn") or "").upper().strip() == mpn_upper:
                site_name = ""
                if q.customer_site:
                    site_name = (
                        f"{q.customer_site.company.name} — {q.customer_site.site_name}"
                        if q.customer_site.company else q.customer_site.site_name
                    )
                history.append({
                    "date": (q.sent_at or q.created_at).isoformat() if (q.sent_at or q.created_at) else None,
                    "qty": item.get("qty"),
                    "sell_price": item.get("sell_price"),
                    "margin_pct": item.get("margin_pct"),
                    "customer": site_name,
                    "result": q.result,
                    "quote_number": q.quote_number,
                })
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
async def clone_requisition(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
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
            primary_mpn=r.primary_mpn, oem_pn=r.oem_pn,
            brand=r.brand, sku=r.sku,
            target_qty=r.target_qty, target_price=r.target_price,
            substitutes=r.substitutes, notes=r.notes,
        )
        db.add(new_r)
    db.flush()
    # Map old requirement IDs → new for offer cloning
    req_map: dict[int, int] = {}
    for old_r in req.requirements:
        new_r = db.query(Requirement).filter(
            Requirement.requisition_id == new_req.id,
            Requirement.primary_mpn == old_r.primary_mpn,
        ).first()
        if new_r:
            req_map[old_r.id] = new_r.id
    for o in req.offers:
        if o.status in ("active", "selected"):
            new_o = Offer(
                requisition_id=new_req.id,
                requirement_id=req_map.get(o.requirement_id),
                vendor_card_id=o.vendor_card_id, vendor_name=o.vendor_name,
                mpn=o.mpn, manufacturer=o.manufacturer,
                qty_available=o.qty_available, unit_price=o.unit_price,
                lead_time=o.lead_time, date_code=o.date_code,
                condition=o.condition, packaging=o.packaging,
                moq=o.moq, source=o.source,
                entered_by_id=user.id,
                notes=f"Reference from REQ-{req.id:03d}",
                status="reference",
            )
            db.add(new_o)
    db.commit()
    return {"id": new_req.id, "name": new_req.name}
