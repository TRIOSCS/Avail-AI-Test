import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ...config import settings
from ...database import get_db
from ...dependencies import require_user
from ...models import Company, CustomerSite, Requisition, SiteContact, User
from ...schemas.crm import CompanyCreate, CompanyUpdate
from ...services.credential_service import get_credential_cached

router = APIRouter()


# ── Companies ────────────────────────────────────────────────────────────


@router.get("/api/companies")
async def list_companies(
    search: str = "",
    owner_id: int = 0,
    unassigned: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List active companies with sites and open requisition counts."""
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


@router.get("/api/companies/typeahead")
async def companies_typeahead(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lightweight endpoint returning all active companies + site IDs for the
    requisition creation typeahead. No limit, minimal payload."""
    companies = (
        db.query(Company)
        .filter(Company.is_active == True)  # noqa: E712
        .options(selectinload(Company.sites))
        .order_by(Company.name)
        .all()
    )
    return [
        {
            "id": c.id,
            "name": c.name,
            "sites": [
                {"id": s.id, "site_name": s.site_name}
                for s in c.sites
                if s.is_active
            ],
        }
        for c in companies
    ]


@router.get("/api/companies/check-duplicate")
async def check_company_duplicate(
    name: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Check if a company name is a near-duplicate of an existing company.

    Normalizes to lowercase, strips suffixes (Inc, LLC, Ltd, Corp, etc.),
    and compares for matches.
    """
    _suffixes = re.compile(
        r"\b(inc\.?|llc\.?|ltd\.?|corp\.?|co\.?|plc\.?|gmbh|ag|sa|s\.?a\.?|"
        r"s\.?r\.?l\.?|pty\.?|b\.?v\.?|n\.?v\.?|a\.?s\.?|oy|ab|limited|"
        r"corporation|incorporated|company)\s*$",
        re.IGNORECASE,
    )

    def _normalize(n: str) -> str:
        n = n.strip().lower()
        n = re.sub(r"[^\w\s]", " ", n)  # punctuation -> space
        n = _suffixes.sub("", n).strip()
        n = re.sub(r"\s+", " ", n)
        return n

    clean = _normalize(name)
    if not clean:
        return {"matches": []}

    # Pull all company names (cached at 500 limit, same as list_companies)
    companies = (
        db.query(Company.id, Company.name)
        .filter(Company.is_active == True)  # noqa: E712
        .limit(2000)
        .all()
    )
    matches = []
    for c in companies:
        cn = _normalize(c.name)
        if not cn:
            continue
        # Exact normalized match
        if cn == clean:
            matches.append({"id": c.id, "name": c.name, "match": "exact"})
        # Containment (one is substring of the other)
        elif cn in clean or clean in cn:
            matches.append({"id": c.id, "name": c.name, "match": "similar"})
        # Prefix match (first 6+ chars match)
        elif len(clean) >= 6 and len(cn) >= 6 and cn[:6] == clean[:6]:
            matches.append({"id": c.id, "name": c.name, "match": "similar"})
    return {"matches": matches[:5]}


@router.post("/api/companies")
async def create_company(
    payload: CompanyCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...enrichment_service import normalize_company_input

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
    db.flush()  # get company.id before creating site

    # Auto-create default "HQ" site so company appears in req typeahead
    default_site = CustomerSite(company_id=company.id, site_name="HQ")
    db.add(default_site)
    db.commit()

    # Auto-enrich if domain is available
    enrich_triggered = False
    domain = company.domain or ""
    if domain and (
        get_credential_cached("clay_enrichment", "CLAY_API_KEY")
        or get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        or get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
        or getattr(settings, "apollo_api_key", "")
        or getattr(settings, "hunter_api_key", "")
        or getattr(settings, "do_gradient_api_key", "")
    ):
        from ...enrichment_service import apply_enrichment_to_company, enrich_entity

        async def _enrich_company_bg(cid, sid, d, n):
            from ...database import SessionLocal

            try:
                enrichment = await enrich_entity(d, n)
                s = SessionLocal()
                try:
                    if enrichment:
                        c = s.get(Company, cid)
                        if c:
                            apply_enrichment_to_company(c, enrichment)
                            s.commit()

                    # Auto-discover contacts and attach to default site
                    from ...enrichment_service import find_suggested_contacts
                    from ...models import SiteContact
                    contacts = await find_suggested_contacts(d, n)
                    if contacts:
                        existing_emails = {
                            sc.email.lower()
                            for sc in s.query(SiteContact).filter(
                                SiteContact.customer_site_id == sid,
                                SiteContact.email.isnot(None),
                            ).all()
                        }
                        added = 0
                        for ct in contacts:
                            email = (ct.get("email") or "").lower()
                            if email and email not in existing_emails:
                                s.add(SiteContact(
                                    customer_site_id=sid,
                                    full_name=ct.get("full_name"),
                                    title=ct.get("title"),
                                    email=email,
                                    phone=ct.get("phone"),
                                ))
                                existing_emails.add(email)
                                added += 1
                        if added:
                            s.commit()
                            logger.info("Auto-discovered %d contacts for company %d", added, cid)
                finally:
                    s.close()
            except Exception:
                logger.exception("Background enrichment failed for company %d", cid)

        asyncio.create_task(_enrich_company_bg(company.id, default_site.id, domain, company.name))
        enrich_triggered = True

    return {
        "id": company.id,
        "name": company.name,
        "default_site_id": default_site.id,
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
        raise HTTPException(404, "Company not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(company, field, value)
    db.commit()
    return {"ok": True}
