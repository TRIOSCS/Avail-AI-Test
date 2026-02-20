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

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import settings
from ..database import get_db
from ..dependencies import is_admin as _is_admin
from ..dependencies import require_admin, require_buyer, require_user
from ..models import (
    ActivityLog,
    BuyPlan,
    Company,
    CustomerSite,
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
from ..schemas.crm import (
    AddContactsToVendor,
    AddContactToSite,
    BuyPlanApprove,
    BuyPlanCancel,
    BuyPlanPOBulk,
    BuyPlanPOEntry,
    BuyPlanReject,
    BuyPlanResubmit,
    BuyPlanSubmit,
    CompanyCreate,
    CompanyUpdate,
    EnrichDomainRequest,
    OfferCreate,
    OfferUpdate,
    OneDriveAttach,
    QuoteCreate,
    QuoteReopen,
    QuoteResult,
    QuoteSendOverride,
    QuoteUpdate,
    SiteContactCreate,
    SiteContactUpdate,
    SiteCreate,
    SiteUpdate,
)
from ..services.credential_service import get_credential_cached
from ..utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
)
from ..vendor_utils import normalize_vendor_name

router = APIRouter()


# ── Helpers ──────────────────────────────────────────────────────────────


from ..services.crm_service import next_quote_number  # noqa: E402


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


def _preload_last_quoted_prices(db: Session) -> dict[str, dict]:
    """Load recent quotes ONCE and build MPN→price lookup dict."""
    quotes = (
        db.query(Quote)
        .filter(Quote.status.in_(["sent", "won", "lost"]))
        .order_by(Quote.sent_at.desc().nullslast(), Quote.created_at.desc())
        .limit(100)
        .all()
    )
    result: dict[str, dict] = {}
    for q in quotes:
        for item in q.line_items or []:
            mpn_key = (item.get("mpn") or "").upper().strip()
            if mpn_key and mpn_key not in result:
                result[mpn_key] = {
                    "sell_price": item.get("sell_price"),
                    "margin_pct": item.get("margin_pct"),
                    "quote_number": q.quote_number,
                    "date": (q.sent_at or q.created_at).isoformat()
                    if (q.sent_at or q.created_at)
                    else None,
                    "result": q.result,
                }
    return result


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
        "site_contacts": [
            {
                "id": c.id,
                "full_name": c.full_name,
                "email": c.email,
                "title": c.title,
                "is_primary": c.is_primary,
            }
            for c in (q.customer_site.site_contacts if q.customer_site else [])
        ],
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
    unassigned: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    query = (
        db.query(Company)
        .filter(Company.is_active == True)  # noqa: E712
        .options(
            selectinload(Company.sites).joinedload(CustomerSite.owner),
            joinedload(Company.account_owner),
        )
    )
    if search.strip():
        safe = search.strip().replace("%", r"\%").replace("_", r"\_")
        query = query.filter(Company.name.ilike(f"%{safe}%"))
    if unassigned:
        query = query.filter(Company.account_owner_id == None)  # noqa: E711
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
            if owner_id and not unassigned and s.owner_id != owner_id:
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
        if owner_id and not unassigned and not sites:
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
                "account_type": c.account_type,
                "phone": c.phone,
                "credit_terms": c.credit_terms,
                "tax_id": c.tax_id,
                "currency": c.currency,
                "preferred_carrier": c.preferred_carrier,
                "is_strategic": c.is_strategic,
                "account_owner_id": c.account_owner_id,
                "account_owner_name": (c.account_owner.name if c.account_owner else None) if c.account_owner_id else None,
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
        get_credential_cached("clay_enrichment", "CLAY_API_KEY")
        or get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        or get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
    ):
        from ..enrichment_service import apply_enrichment_to_company, enrich_entity

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
        not get_credential_cached("clay_enrichment", "CLAY_API_KEY")
        and not get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        and not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
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
        not get_credential_cached("clay_enrichment", "CLAY_API_KEY")
        and not get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        and not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
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
        not get_credential_cached("clay_enrichment", "CLAY_API_KEY")
        and not get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        and not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
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

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, run_sync, db)
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


from ..rate_limit import limiter


@router.post("/api/customers/import")
@limiter.limit("5/minute")
async def import_customers(
    request: Request,
    user: User = Depends(require_admin),
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
        .options(
            joinedload(Offer.entered_by),
            selectinload(Offer.attachments),
        )
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
    # Preload quoted prices ONCE instead of per-requirement DB call
    quoted_prices = _preload_last_quoted_prices(db)
    result = []
    for r in req.requirements:
        target = float(r.target_price) if r.target_price else None
        last_q = quoted_prices.get((r.primary_mpn or "").upper().strip())
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
            get_credential_cached("clay_enrichment", "CLAY_API_KEY")
            or get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
            or get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
        ):
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

    # Teams: competitive quote alert if >20% below current best price
    try:
        if offer.unit_price and offer.unit_price > 0 and offer.requirement_id:
            from sqlalchemy import func
            best_price = (
                db.query(func.min(Offer.unit_price))
                .filter(
                    Offer.requirement_id == offer.requirement_id,
                    Offer.id != offer.id,
                    Offer.unit_price > 0,
                )
                .scalar()
            )
            if best_price and float(offer.unit_price) < float(best_price) * 0.8:
                pct = round((1 - float(offer.unit_price) / float(best_price)) * 100)
                from ..services.teams import send_competitive_quote_alert
                asyncio.create_task(send_competitive_quote_alert(
                    offer_id=offer.id,
                    mpn=offer.mpn,
                    vendor_name=offer.vendor_name,
                    offer_price=float(offer.unit_price),
                    best_price=float(best_price),
                    requisition_id=req_id,
                ))
                # In-app notification for requisition owner
                if req.created_by:
                    db.add(ActivityLog(
                        user_id=req.created_by,
                        activity_type="competitive_quote",
                        channel="system",
                        requisition_id=req_id,
                        contact_name=offer.vendor_name,
                        subject=f"Competitive quote: {offer.vendor_name} — {offer.mpn} at ${offer.unit_price} ({pct}% below best)",
                    ))
                    db.commit()
    except Exception:
        logger.debug("Activity event creation failed", exc_info=True)

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
    user: User = Depends(require_buyer),
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
    offer_id: int, user: User = Depends(require_buyer), db: Session = Depends(get_db)
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
    from ..http_client import http

    resp = await http.put(
        f"https://graph.microsoft.com/v1.0{drive_path}",
        content=content,
        headers={
            "Authorization": f"Bearer {user.access_token}",
            "Content-Type": file.content_type or "application/octet-stream",
        },
        timeout=30,
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
    body: OneDriveAttach,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Attach an existing OneDrive file to an offer by item ID."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404)
    item_id = body.item_id
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
            from ..http_client import http

            await http.delete(
                f"https://graph.microsoft.com/v1.0/me/drive/items/{att.onedrive_item_id}",
                headers={"Authorization": f"Bearer {user.access_token}"},
                timeout=15,
            )
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.warning(f"Failed to delete OneDrive item {att.onedrive_item_id}: {e}")
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
        .options(
            joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(Quote.customer_site).joinedload(CustomerSite.site_contacts),
            joinedload(Quote.created_by),
        )
        .filter(
            Quote.requisition_id == req_id,
        )
        .order_by(Quote.revision.desc())
        .first()
    )
    if not quote:
        return None
    return quote_to_dict(quote)


@router.get("/api/requisitions/{req_id}/quotes")
async def list_quotes(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """List all quotes (including revisions) for a requisition."""
    from ..dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    quotes = (
        db.query(Quote)
        .options(
            joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(Quote.customer_site).joinedload(CustomerSite.site_contacts),
            joinedload(Quote.created_by),
        )
        .filter(Quote.requisition_id == req_id)
        .order_by(Quote.revision.desc())
        .all()
    )
    return [quote_to_dict(q) for q in quotes]


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
        quoted_prices = _preload_last_quoted_prices(db)
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
                lq = quoted_prices.get((o.mpn or "").upper().strip())
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
                    "date_code": o.date_code,
                    "firmware": o.firmware,
                    "hardware_code": o.hardware_code,
                    "packaging": o.packaging,
                    "offer_id": o.id,
                    "target_price": target,
                    "last_quoted_price": last_q_price,
                }
            )
    site = db.get(CustomerSite, req.customer_site_id)
    total_sell = sum(
        (item.get("qty") or 0) * (item.get("sell_price") or 0)
        for item in line_items
    )
    total_cost = sum(
        (item.get("qty") or 0) * (item.get("cost_price") or 0)
        for item in line_items
    )
    quote = Quote(
        requisition_id=req_id,
        customer_site_id=req.customer_site_id,
        quote_number=next_quote_number(db),
        line_items=line_items,
        subtotal=total_sell,
        total_cost=total_cost,
        total_margin_pct=(
            round((total_sell - total_cost) / total_sell * 100, 2)
            if total_sell > 0
            else 0
        ),
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
    # Eager-load relations for serialization
    quote = (
        db.query(Quote)
        .options(
            joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(Quote.customer_site).joinedload(CustomerSite.site_contacts),
            joinedload(Quote.created_by),
        )
        .filter(Quote.id == quote.id)
        .first()
    )
    return quote_to_dict(quote)


@router.post("/api/quotes/{quote_id}/send")
async def send_quote(
    quote_id: int,
    request: Request,
    body: QuoteSendOverride | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..dependencies import require_fresh_token

    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404)

    # Allow caller to override recipient email/name
    override_email = ((body.to_email if body else None) or "").strip()
    override_name = ((body.to_name if body else None) or "").strip()

    site = db.get(CustomerSite, quote.customer_site_id)
    to_email = override_email or (site.contact_email if site else None)
    if not to_email:
        raise HTTPException(400, "No recipient email — select a contact or enter one manually")

    to_name = override_name or (site.contact_name if site else "") or ""
    company_name = site.company.name if site and site.company else ""

    # Build the HTML quote email
    html = _build_quote_email_html(quote, to_name, company_name, user)

    subject = f"Quote {quote.quote_number} — Trio Supply Chain Solutions"

    # Send via Graph API
    token = await require_fresh_token(request, db)
    from app.utils.graph_client import GraphClient

    gc = GraphClient(token)
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [
                {"emailAddress": {"address": to_email, "name": to_name}}
            ],
        },
        "saveToSentItems": "true",
    }
    result = await gc.post_json("/me/sendMail", payload)
    if "error" in result:
        raise HTTPException(502, f"Failed to send quote email: {result.get('detail', '')}")

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
        "sent_to": to_email,
        "req_status": req.status if req else None,
        "status_changed": req and req.status != old_status,
    }


def _build_quote_email_html(quote: Quote, to_name: str, company_name: str, user: User) -> str:
    """Build a professional HTML quote email with Trio branding."""
    from datetime import timedelta

    BLUE = "#127fbf"
    DARK = "#1a2a3a"

    validity = quote.validity_days or 7
    now_ts = quote.sent_at or datetime.now(timezone.utc)
    expires = now_ts + timedelta(days=validity)
    expires_str = expires.strftime("%B %d, %Y")
    date_str = now_ts.strftime("%B %d, %Y")

    # Build line items rows
    rows = ""
    row_bg = ["#ffffff", "#f8fafc"]
    for idx, item in enumerate(quote.line_items or []):
        price = f"${item.get('sell_price', 0):,.4f}" if item.get("sell_price") else "—"
        qty = f"{item.get('qty', 0):,}" if item.get("qty") else "—"
        ext = f"${item.get('sell_price', 0) * item.get('qty', 0):,.2f}" if item.get("sell_price") and item.get("qty") else "—"
        cond = item.get("condition") or "—"
        dc = item.get("date_code") or "—"
        pkg = item.get("packaging") or "—"
        bg = row_bg[idx % 2]
        td = f'style="padding:10px 12px;border-bottom:1px solid #e8ecf0;background:{bg}"'
        rows += f"""<tr>
            <td {td}><strong>{item.get('mpn','')}</strong></td>
            <td {td}>{item.get('manufacturer','') or '—'}</td>
            <td {td} style="padding:10px 12px;border-bottom:1px solid #e8ecf0;background:{bg};text-align:center">{qty}</td>
            <td {td}>{cond}</td>
            <td {td}>{dc}</td>
            <td {td}>{pkg}</td>
            <td {td} style="padding:10px 12px;border-bottom:1px solid #e8ecf0;background:{bg};text-align:right">{price}</td>
            <td {td} style="padding:10px 12px;border-bottom:1px solid #e8ecf0;background:{bg};text-align:right">{item.get('lead_time','') or '—'}</td>
            <td {td} style="padding:10px 12px;border-bottom:1px solid #e8ecf0;background:{bg};text-align:right;font-weight:600">{ext}</td>
        </tr>"""

    total = f"${float(quote.subtotal or 0):,.2f}"

    # Terms table
    terms_rows = ""
    if quote.payment_terms:
        terms_rows += f'<tr><td style="padding:6px 0;color:#666;width:120px">Payment</td><td style="padding:6px 0;font-weight:600">{quote.payment_terms}</td></tr>'
    if quote.shipping_terms:
        terms_rows += f'<tr><td style="padding:6px 0;color:#666">Shipping</td><td style="padding:6px 0;font-weight:600">{quote.shipping_terms}</td></tr>'
    terms_rows += '<tr><td style="padding:6px 0;color:#666">Currency</td><td style="padding:6px 0;font-weight:600">USD</td></tr>'
    terms_rows += f'<tr><td style="padding:6px 0;color:#666">Valid Until</td><td style="padding:6px 0;font-weight:600">{expires_str}</td></tr>'

    greeting = f"Dear {to_name}," if to_name else "Dear Valued Customer,"
    notes_block = f'<div style="margin-top:16px;padding:12px 16px;background:#f0f7ff;border-left:3px solid {BLUE};border-radius:4px;font-size:13px;color:#444">{quote.notes}</div>' if quote.notes else ""
    signature = user.email_signature or f"{user.name or 'Trio Supply Chain Solutions'}"
    sig_html = signature.replace("\n", "<br>")

    th = f'style="padding:10px 12px;text-align:left;border-bottom:2px solid {BLUE};font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:{DARK}"'

    return f"""<html><body style="margin:0;padding:0;background:#f4f6f9;font-family:Calibri,Arial,Helvetica,sans-serif;color:#333">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:20px 0">
<tr><td align="center">
<table width="700" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

<!-- Header -->
<tr><td style="background:{BLUE};padding:24px 32px;text-align:center">
    <img src="https://www.trioscs.com/wp-content/uploads/2022/02/TRIO_CV_400.png" alt="Trio Supply Chain Solutions" style="height:50px;display:inline-block">
</td></tr>

<!-- Quote Info Bar -->
<tr><td style="background:{DARK};padding:12px 32px">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="color:#ffffff;font-size:18px;font-weight:700">Quotation</td>
        <td style="text-align:right;color:#c0d0e0;font-size:13px">
            <strong style="color:#ffffff">{quote.quote_number}</strong> &nbsp;Rev {quote.revision}
            &nbsp;&middot;&nbsp; {date_str}
        </td>
    </tr></table>
</td></tr>

<!-- Body -->
<tr><td style="padding:28px 32px">
    <p style="margin:0 0 4px;font-size:15px;color:#333">{greeting}</p>
    <p style="margin:0 0 20px;font-size:14px;color:#666">Thank you for your interest. Please find our quotation for <strong>{company_name or 'your company'}</strong> below.</p>

    <!-- Line Items Table -->
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:12px;margin-bottom:16px">
        <thead>
            <tr style="background:#f0f4f8">
                <th {th}>Part Number</th>
                <th {th}>Mfr</th>
                <th {th} style="padding:10px 12px;text-align:center;border-bottom:2px solid {BLUE};font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:{DARK}">Qty</th>
                <th {th}>Cond</th>
                <th {th}>Date Code</th>
                <th {th}>Pkg</th>
                <th {th} style="padding:10px 12px;text-align:right;border-bottom:2px solid {BLUE};font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:{DARK}">Unit Price</th>
                <th {th} style="padding:10px 12px;text-align:right;border-bottom:2px solid {BLUE};font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:{DARK}">Lead Time</th>
                <th {th} style="padding:10px 12px;text-align:right;border-bottom:2px solid {BLUE};font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:{DARK}">Ext. Price</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
        <tfoot>
            <tr>
                <td colspan="8" style="padding:12px;text-align:right;font-size:14px;font-weight:600;color:#666;border-top:2px solid {BLUE}">Total</td>
                <td style="padding:12px;text-align:right;font-size:16px;font-weight:700;color:{BLUE};border-top:2px solid {BLUE}">{total}</td>
            </tr>
        </tfoot>
    </table>

    <!-- Terms -->
    <table cellpadding="0" cellspacing="0" style="font-size:13px;margin-bottom:16px">
        {terms_rows}
    </table>

    {notes_block}

    <!-- Signature -->
    <div style="margin-top:24px;padding-top:16px;border-top:1px solid #e8ecf0">
        <p style="margin:0;font-size:13px;color:#555">{sig_html}</p>
    </div>
</td></tr>

<!-- Terms & Conditions -->
<tr><td style="padding:20px 32px;background:#f8fafc;border-top:1px solid #e8ecf0">
    <p style="margin:0 0 8px;font-size:11px;font-weight:700;color:{DARK};text-transform:uppercase;letter-spacing:0.5px">Terms &amp; Conditions</p>
    <ol style="margin:0;padding-left:16px;font-size:10px;color:#777;line-height:1.7">
        <li>This quotation is valid for the period stated above. Prices are subject to change after expiration.</li>
        <li>All prices are in USD unless otherwise stated. Sales tax is not included and will be applied where applicable.</li>
        <li>Payment terms are as stated above. Past-due invoices are subject to a 1.5% monthly finance charge.</li>
        <li>Delivery dates are estimated and subject to availability at time of order confirmation.</li>
        <li>All sales are subject to Trio Supply Chain Solutions' standard terms and conditions of sale.</li>
        <li>Cancellation or rescheduling of confirmed orders may be subject to restocking and/or cancellation fees.</li>
        <li>Warranty: Parts are warranted against defects for 90 days from date of shipment. Warranty does not cover misuse, modification, or improper installation.</li>
        <li>Trio Supply Chain Solutions shall not be liable for any indirect, incidental, or consequential damages arising from the sale or use of products.</li>
        <li>Export compliance: Buyer is responsible for compliance with all applicable export control laws and regulations.</li>
    </ol>
</td></tr>

<!-- Footer -->
<tr><td style="background:{DARK};padding:16px 32px;text-align:center">
    <p style="margin:0;font-size:12px;color:#8899aa">Trio Supply Chain Solutions</p>
    <p style="margin:4px 0 0;font-size:11px;color:#667788">
        <a href="https://trioscs.com" style="color:{BLUE};text-decoration:none">trioscs.com</a>
        &nbsp;&middot;&nbsp; info@trioscs.com
    </p>
</td></tr>

</table>
</td></tr>
</table>
</body></html>"""


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
    # Gather deal context from quote/requisition
    customer_name = ""
    quote_number = ""
    quote_subtotal = None
    if bp.quote:
        quote_number = bp.quote.quote_number or ""
        quote_subtotal = float(bp.quote.subtotal) if bp.quote.subtotal else None
        if bp.quote.customer_site and bp.quote.customer_site.company:
            customer_name = (
                f"{bp.quote.customer_site.company.name} — "
                f"{bp.quote.customer_site.site_name}"
            )

    # Compute margin totals from line items
    total_cost = 0.0
    total_revenue = 0.0
    for item in bp.line_items or []:
        plan_qty = item.get("plan_qty") or item.get("qty") or 0
        cost = plan_qty * (item.get("cost_price") or 0)
        sell = plan_qty * (item.get("sell_price") or 0)
        total_cost += cost
        total_revenue += sell
    total_profit = total_revenue - total_cost
    overall_margin_pct = (
        round((total_profit / total_revenue) * 100, 2) if total_revenue else 0
    )

    return {
        "id": bp.id,
        "requisition_id": bp.requisition_id,
        "requisition_name": bp.requisition.name if bp.requisition else None,
        "quote_id": bp.quote_id,
        "quote_number": quote_number,
        "quote_subtotal": quote_subtotal,
        "customer_name": customer_name,
        "status": bp.status,
        "line_items": bp.line_items or [],
        "is_stock_sale": bp.is_stock_sale or False,
        "total_cost": round(total_cost, 2),
        "total_revenue": round(total_revenue, 2),
        "total_profit": round(total_profit, 2),
        "overall_margin_pct": overall_margin_pct,
        "sales_order_number": bp.sales_order_number,
        "salesperson_notes": bp.salesperson_notes,
        "manager_notes": bp.manager_notes,
        "rejection_reason": bp.rejection_reason,
        "submitted_by": bp.submitted_by.name if bp.submitted_by else None,
        "submitted_by_id": bp.submitted_by_id,
        "approved_by": bp.approved_by.name if bp.approved_by else None,
        "approved_by_id": bp.approved_by_id,
        "rejected_by": bp.approved_by.name if (bp.approved_by and bp.status == "rejected") else None,
        "rejected_by_id": bp.approved_by_id if bp.status == "rejected" else None,
        "submitted_at": bp.submitted_at.isoformat() if bp.submitted_at else None,
        "approved_at": bp.approved_at.isoformat() if bp.approved_at else None,
        "rejected_at": bp.rejected_at.isoformat() if bp.rejected_at else None,
        "completed_at": bp.completed_at.isoformat() if bp.completed_at else None,
        "completed_by": bp.completed_by.name if bp.completed_by else None,
        "cancelled_at": bp.cancelled_at.isoformat() if bp.cancelled_at else None,
        "cancelled_by": bp.cancelled_by.name if bp.cancelled_by else None,
        "cancellation_reason": bp.cancellation_reason,
    }


@router.post("/api/quotes/{quote_id}/buy-plan")
async def submit_buy_plan(
    quote_id: int,
    body: BuyPlanSubmit,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a buy plan when marking a quote as Won."""
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404)
    offer_ids = body.offer_ids
    if not offer_ids:
        raise HTTPException(400, "At least one offer must be selected")
    salesperson_notes = body.salesperson_notes.strip()
    plan_qtys = body.plan_qtys

    # Build buy plan line items from selected offers
    offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
    line_items = []
    for o in offers:
        qty_available = o.qty_available or 0
        # Use salesperson's planned qty if provided, otherwise default to available
        plan_qty = plan_qtys.get(str(o.id), plan_qtys.get(o.id, qty_available))
        line_items.append(
            {
                "offer_id": o.id,
                "mpn": o.mpn,
                "vendor_name": o.vendor_name,
                "manufacturer": o.manufacturer,
                "qty": qty_available,
                "plan_qty": int(plan_qty) if plan_qty else qty_available,
                "cost_price": float(o.unit_price) if o.unit_price else 0,
                "lead_time": o.lead_time,
                "condition": o.condition,
                "entered_by_id": o.entered_by_id,
                "po_number": None,
                "po_entered_at": None,
                "po_sent_at": None,
                "po_recipient": None,
                "po_verified": False,
            }
        )

    import secrets

    # Detect stock sale: all vendors match stock_sale_vendor_names
    stock_names = settings.stock_sale_vendor_names
    is_stock = bool(line_items) and all(
        (item.get("vendor_name") or "").strip().lower() in stock_names
        for item in line_items
    )

    plan = BuyPlan(
        requisition_id=quote.requisition_id,
        quote_id=quote_id,
        status="pending_approval",
        salesperson_notes=salesperson_notes or None,
        line_items=line_items,
        submitted_by_id=user.id,
        approval_token=secrets.token_urlsafe(32),
        is_stock_sale=is_stock,
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

    # Mark selected offers as "won" so engagement scorer counts wins
    for o in offers:
        o.status = "won"

    from ..services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(
        db, user.id, plan, "buyplan_submitted", f"submitted for quote #{quote_id}"
    )
    db.commit()

    # Send notifications asynchronously (own DB session)
    from ..services.buyplan_service import notify_buyplan_submitted, run_buyplan_bg

    run_buyplan_bg(notify_buyplan_submitted, plan.id)

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
        if user.role in ("sales", "trader"):
            query = query.filter(BuyPlan.submitted_by_id == user.id)
        # Buyers see all (they need to check which plans have their offers)
    plans = query.limit(200).all()
    return [_buyplan_to_dict(p) for p in plans]


@router.get("/api/buy-plans/token/{token}")
async def get_buyplan_by_token(
    token: str,
    db: Session = Depends(get_db),
):
    """Public: get buy plan by approval token (no auth required)."""
    plan = db.query(BuyPlan).filter(BuyPlan.approval_token == token).first()
    if not plan:
        raise HTTPException(404, "Invalid or expired token")
    return _buyplan_to_dict(plan)


@router.put("/api/buy-plans/token/{token}/approve")
async def approve_buyplan_by_token(
    token: str,
    body: BuyPlanApprove,
    request: Request,
    db: Session = Depends(get_db),
):
    """Public: approve buy plan via token link in email."""
    plan = db.query(BuyPlan).filter(BuyPlan.approval_token == token).first()
    if not plan:
        raise HTTPException(404, "Invalid or expired token")
    if plan.status != "pending_approval":
        raise HTTPException(400, f"Cannot approve plan in status: {plan.status}")

    so_number = body.sales_order_number.strip()
    if not so_number:
        raise HTTPException(400, "Acctivate Sales Order # is required")

    plan.sales_order_number = so_number
    if body.manager_notes is not None:
        plan.manager_notes = body.manager_notes

    plan.status = "approved"
    plan.approved_at = datetime.now(timezone.utc)
    # approved_by_id stays None (token-based, no logged-in user)

    from ..services.buyplan_service import log_buyplan_activity

    # Stock sale fast-track: approve → complete (no PO required)
    if plan.is_stock_sale:
        plan.status = "complete"
        plan.completed_at = datetime.now(timezone.utc)
        # completed_by_id stays None (token-based)
        log_buyplan_activity(
            db, plan.submitted_by_id, plan, "buyplan_approved",
            "stock sale approved + auto-completed via email token",
        )
    else:
        log_buyplan_activity(
            db, plan.submitted_by_id, plan, "buyplan_approved", "approved via email token"
        )
    db.commit()

    from ..services.buyplan_service import notify_buyplan_approved, run_buyplan_bg

    if plan.is_stock_sale:
        from ..services.buyplan_service import notify_stock_sale_approved

        run_buyplan_bg(notify_stock_sale_approved, plan.id)
    else:
        run_buyplan_bg(notify_buyplan_approved, plan.id)
    return {"ok": True, "status": plan.status}


@router.put("/api/buy-plans/token/{token}/reject")
async def reject_buyplan_by_token(
    token: str,
    body: BuyPlanReject,
    request: Request,
    db: Session = Depends(get_db),
):
    """Public: reject buy plan via token link in email."""
    plan = db.query(BuyPlan).filter(BuyPlan.approval_token == token).first()
    if not plan:
        raise HTTPException(404, "Invalid or expired token")
    if plan.status != "pending_approval":
        raise HTTPException(400, f"Cannot reject plan in status: {plan.status}")

    plan.rejection_reason = body.reason
    plan.status = "rejected"
    plan.rejected_at = datetime.now(timezone.utc)

    from ..services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(
        db, plan.submitted_by_id, plan, "buyplan_rejected", "rejected via email token"
    )
    db.commit()

    from ..services.buyplan_service import notify_buyplan_rejected, run_buyplan_bg

    run_buyplan_bg(notify_buyplan_rejected, plan.id)
    return {"ok": True, "status": "rejected"}


@router.get("/api/buy-plans/{plan_id}")
async def get_buy_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    if not _is_admin(user) and user.role not in ("manager", "buyer"):
        if plan.submitted_by_id != user.id:
            raise HTTPException(403, "You can only view your own buy plans")
    return _buyplan_to_dict(plan)


@router.put("/api/buy-plans/{plan_id}/approve")
async def approve_buy_plan(
    plan_id: int,
    body: BuyPlanApprove,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager/admin approves the buy plan."""
    if user.role not in ("admin", "manager"):
        raise HTTPException(403, "Manager or admin approval required")
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    if plan.status != "pending_approval":
        raise HTTPException(400, f"Cannot approve plan in status: {plan.status}")

    so_number = body.sales_order_number.strip()
    if not so_number:
        raise HTTPException(400, "Acctivate Sales Order # is required")
    plan.sales_order_number = so_number

    if body.line_items is not None:
        plan.line_items = body.line_items
    if body.manager_notes is not None:
        plan.manager_notes = body.manager_notes

    plan.status = "approved"
    plan.approved_by_id = user.id
    plan.approved_at = datetime.now(timezone.utc)

    from ..services.buyplan_service import log_buyplan_activity

    # Stock sale fast-track: approve → complete (no PO required)
    if plan.is_stock_sale:
        plan.status = "complete"
        plan.completed_at = datetime.now(timezone.utc)
        plan.completed_by_id = user.id
        log_buyplan_activity(
            db, user.id, plan, "buyplan_approved",
            f"stock sale approved + auto-completed with SO# {so_number}",
        )
    else:
        log_buyplan_activity(
            db, user.id, plan, "buyplan_approved", f"approved with SO# {so_number}"
        )
    db.commit()

    from ..services.buyplan_service import notify_buyplan_approved, run_buyplan_bg

    if plan.is_stock_sale:
        from ..services.buyplan_service import notify_stock_sale_approved

        run_buyplan_bg(notify_stock_sale_approved, plan.id)
    else:
        run_buyplan_bg(notify_buyplan_approved, plan.id)

    return {"ok": True, "status": plan.status}


@router.put("/api/buy-plans/{plan_id}/reject")
async def reject_buy_plan(
    plan_id: int,
    body: BuyPlanReject,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager/admin rejects the buy plan."""
    if user.role not in ("admin", "manager"):
        raise HTTPException(403, "Manager or admin rejection required")
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    if plan.status != "pending_approval":
        raise HTTPException(400, f"Cannot reject plan in status: {plan.status}")

    plan.rejection_reason = body.reason
    plan.status = "rejected"
    plan.approved_by_id = user.id  # reuse field for who acted
    plan.rejected_at = datetime.now(timezone.utc)

    from ..services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(
        db, user.id, plan, "buyplan_rejected", plan.rejection_reason or "no reason"
    )
    db.commit()

    from ..services.buyplan_service import notify_buyplan_rejected, run_buyplan_bg

    run_buyplan_bg(notify_buyplan_rejected, plan.id)

    return {"ok": True, "status": "rejected"}


@router.put("/api/buy-plans/{plan_id}/po")
async def enter_po_number(
    plan_id: int,
    body: BuyPlanPOEntry,
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

    line_index = body.line_index
    po_number = body.po_number.strip()
    if not po_number:
        raise HTTPException(400, "po_number required")
    if line_index < 0 or line_index >= len(plan.line_items or []):
        raise HTTPException(400, "Invalid line_index")

    plan.line_items[line_index]["po_number"] = po_number
    plan.line_items[line_index]["po_entered_at"] = datetime.now(timezone.utc).isoformat()
    plan.status = "po_entered"

    from sqlalchemy.orm.attributes import flag_modified

    from ..services.buyplan_service import log_buyplan_activity

    flag_modified(plan, "line_items")
    log_buyplan_activity(
        db, user.id, plan, "buyplan_po_entered", f"line {line_index} PO: {po_number}"
    )
    db.commit()

    # Trigger PO verification in background
    from ..services.buyplan_service import run_buyplan_bg, verify_po_sent

    run_buyplan_bg(verify_po_sent, plan.id)

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


@router.put("/api/buy-plans/{plan_id}/complete")
async def complete_buy_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Admin/manager marks a buy plan as complete."""
    if not _is_admin(user) and user.role != "manager":
        raise HTTPException(403, "Admin or manager required")
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    allowed = ["po_confirmed"]
    if plan.is_stock_sale:
        allowed.append("approved")
    if plan.status not in allowed:
        raise HTTPException(
            400, f"Can only complete from {'/'.join(allowed)}, current: {plan.status}"
        )

    plan.status = "complete"
    plan.completed_at = datetime.now(timezone.utc)
    plan.completed_by_id = user.id

    from ..services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(db, user.id, plan, "buyplan_completed", "marked complete")
    db.commit()

    from ..services.buyplan_service import notify_buyplan_completed, run_buyplan_bg

    run_buyplan_bg(
        notify_buyplan_completed, plan.id,
        completer_name=user.name or user.email,
    )

    return {"ok": True, "status": "complete"}


@router.put("/api/buy-plans/{plan_id}/cancel")
async def cancel_buy_plan(
    plan_id: int,
    body: BuyPlanCancel,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cancel a buy plan. Submitter can cancel from pending_approval.
    Admin/manager can cancel from pending_approval or approved (before POs)."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)

    is_mgr = _is_admin(user) or user.role == "manager"
    is_submitter = plan.submitted_by_id == user.id

    if plan.status == "pending_approval":
        if not is_submitter and not is_mgr:
            raise HTTPException(
                403, "Only submitter or admin/manager can cancel pending plans"
            )
    elif plan.status == "approved":
        if not is_mgr:
            raise HTTPException(403, "Only admin/manager can cancel approved plans")
        has_pos = any(
            item.get("po_number") for item in (plan.line_items or [])
        )
        if has_pos:
            raise HTTPException(
                400, "Cannot cancel — PO numbers already entered. Remove POs first."
            )
    else:
        raise HTTPException(400, f"Cannot cancel plan in status: {plan.status}")

    reason = body.reason.strip()

    plan.status = "cancelled"
    plan.cancelled_at = datetime.now(timezone.utc)
    plan.cancelled_by_id = user.id
    plan.cancellation_reason = reason or None

    # Revert quote/req/offer statuses
    quote = db.get(Quote, plan.quote_id)
    req = db.get(Requisition, plan.requisition_id) if plan.requisition_id else None
    if quote:
        quote.status = "sent"
        quote.result = None
        quote.result_at = None
        quote.won_revenue = None
    if req:
        req.status = "active"
    offer_ids = [
        item.get("offer_id")
        for item in (plan.line_items or [])
        if item.get("offer_id")
    ]
    if offer_ids:
        offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
        for o in offers:
            if o.status == "won":
                o.status = "active"

    from ..services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(
        db, user.id, plan, "buyplan_cancelled", reason or "cancelled"
    )
    db.commit()

    from ..services.buyplan_service import notify_buyplan_cancelled, run_buyplan_bg

    run_buyplan_bg(notify_buyplan_cancelled, plan.id)

    return {"ok": True, "status": "cancelled"}


@router.put("/api/buy-plans/{plan_id}/resubmit")
async def resubmit_buy_plan(
    plan_id: int,
    body: BuyPlanResubmit,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Resubmit a rejected or cancelled buy plan as a new plan."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    if not _is_admin(user) and user.role != "manager":
        if plan.submitted_by_id != user.id:
            raise HTTPException(403, "Only the original submitter, admin, or manager can resubmit")
    if plan.status not in ("rejected", "cancelled"):
        raise HTTPException(
            400, f"Can only resubmit from rejected/cancelled, current: {plan.status}"
        )

    salesperson_notes = body.salesperson_notes.strip()

    import secrets

    new_line_items = [
        {
            **item,
            "po_number": None,
            "po_entered_at": None,
            "po_sent_at": None,
            "po_recipient": None,
            "po_verified": False,
        }
        for item in (plan.line_items or [])
    ]

    # Detect stock sale: all vendors match stock_sale_vendor_names
    stock_names = settings.stock_sale_vendor_names
    is_stock = bool(new_line_items) and all(
        (item.get("vendor_name") or "").strip().lower() in stock_names
        for item in new_line_items
    )

    new_plan = BuyPlan(
        requisition_id=plan.requisition_id,
        quote_id=plan.quote_id,
        status="pending_approval",
        salesperson_notes=salesperson_notes or plan.salesperson_notes,
        line_items=new_line_items,
        submitted_by_id=user.id,
        approval_token=secrets.token_urlsafe(32),
        is_stock_sale=is_stock,
    )
    db.add(new_plan)

    # Re-mark quote/req/offers as won
    quote = db.get(Quote, plan.quote_id)
    req = db.get(Requisition, plan.requisition_id) if plan.requisition_id else None
    if quote:
        quote.status = "won"
        quote.result = "won"
        quote.result_at = datetime.now(timezone.utc)
        quote.won_revenue = quote.subtotal
    if req:
        req.status = "won"
    offer_ids = [
        item.get("offer_id")
        for item in (plan.line_items or [])
        if item.get("offer_id")
    ]
    if offer_ids:
        offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
        for o in offers:
            o.status = "won"

    from ..services.buyplan_service import log_buyplan_activity

    log_buyplan_activity(
        db, user.id, new_plan, "buyplan_resubmitted",
        f"resubmitted from plan #{plan.id}",
    )
    db.commit()

    from ..services.buyplan_service import notify_buyplan_submitted, run_buyplan_bg

    run_buyplan_bg(notify_buyplan_submitted, new_plan.id)

    return {"ok": True, "new_plan_id": new_plan.id, "status": "pending_approval"}


@router.put("/api/buy-plans/{plan_id}/po-bulk")
async def bulk_po_entry(
    plan_id: int,
    body: BuyPlanPOBulk,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk add/edit/clear PO numbers for line items."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    if plan.status not in ("approved", "po_entered"):
        raise HTTPException(
            400, f"Cannot modify POs for plan in status: {plan.status}"
        )

    entries = body.entries
    if not entries:
        raise HTTPException(400, "No PO entries provided")

    from sqlalchemy.orm.attributes import flag_modified

    from ..services.buyplan_service import log_buyplan_activity

    now = datetime.now(timezone.utc).isoformat()
    changes = 0

    for entry in entries:
        idx = entry.line_index
        po = entry.po_number.strip() or None

        if idx is None or idx < 0 or idx >= len(plan.line_items or []):
            continue

        item = plan.line_items[idx]
        old_po = item.get("po_number")

        if po:
            if old_po and old_po != po:
                # Edit: reset verification
                item["po_verified"] = False
                item["po_sent_at"] = None
                item["po_recipient"] = None
                log_buyplan_activity(
                    db, user.id, plan, "buyplan_po_updated",
                    f"line {idx} PO changed: {old_po} -> {po}",
                )
                changes += 1
            elif not old_po:
                log_buyplan_activity(
                    db, user.id, plan, "buyplan_po_entered",
                    f"line {idx} PO: {po}",
                )
                changes += 1
            item["po_number"] = po
            item["po_entered_at"] = now
        else:
            # Clear PO
            if old_po:
                log_buyplan_activity(
                    db, user.id, plan, "buyplan_po_updated",
                    f"line {idx} PO cleared (was {old_po})",
                )
                changes += 1
            item["po_number"] = None
            item["po_entered_at"] = None
            item["po_sent_at"] = None
            item["po_recipient"] = None
            item["po_verified"] = False

    # Determine new status
    has_any_po = any(item.get("po_number") for item in plan.line_items)
    if has_any_po:
        plan.status = "po_entered"
    else:
        plan.status = "approved"

    flag_modified(plan, "line_items")
    db.commit()

    # Trigger verification in background
    if has_any_po:
        from ..services.buyplan_service import run_buyplan_bg, verify_po_sent

        run_buyplan_bg(verify_po_sent, plan.id)

    return {"ok": True, "status": plan.status, "changes": changes}


@router.get("/api/buy-plans/for-quote/{quote_id}")
async def get_buyplan_for_quote(
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get the buy plan associated with a quote (newest if multiple exist)."""
    plan = (
        db.query(BuyPlan)
        .filter(BuyPlan.quote_id == quote_id)
        .order_by(BuyPlan.created_at.desc())
        .first()
    )
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
                        "cost_price": item.get("cost_price"),
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
        cloned_mpn = normalize_mpn(r.primary_mpn) or r.primary_mpn
        # Dedup substitutes by canonical key
        seen_keys = {normalize_mpn_key(cloned_mpn)}
        deduped_subs = []
        for s in (r.substitutes or []):
            ns = normalize_mpn(s) or s
            key = normalize_mpn_key(ns)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped_subs.append(ns)
        new_r = Requirement(
            requisition_id=new_req.id,
            primary_mpn=cloned_mpn,
            normalized_mpn=normalize_mpn_key(cloned_mpn),
            oem_pn=r.oem_pn,
            brand=r.brand,
            sku=r.sku,
            target_qty=r.target_qty,
            target_price=r.target_price,
            substitutes=deduped_subs[:20],
            condition=normalize_condition(r.condition) or r.condition,
            packaging=normalize_packaging(r.packaging) or r.packaging,
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
