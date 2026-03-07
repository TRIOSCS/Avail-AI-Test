import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import String
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ...cache.decorators import cached_endpoint, invalidate_prefix
from ...config import settings
from ...database import get_db
from ...dependencies import require_user
from ...models import Company, CustomerSite, Quote, Requisition, User
from ...schemas.crm import CompanyCreate, CompanyUpdate
from ...services.credential_service import get_credential_cached
from ...utils.sql_helpers import escape_like

router = APIRouter()


def _load_company_tags(company_id: int, db: Session) -> list[dict]:
    """Load visible entity tags for a company."""
    from ...models.tags import EntityTag

    tags = (
        db.query(EntityTag)
        .filter(EntityTag.entity_type == "company", EntityTag.entity_id == company_id, EntityTag.is_visible.is_(True))
        .order_by(EntityTag.interaction_count.desc())
        .all()
    )
    return [
        {"tag_name": et.tag.name, "tag_type": et.tag.tag_type, "count": et.interaction_count, "is_visible": et.is_visible}
        for et in tags
    ]


# ── Companies ────────────────────────────────────────────────────────────


@router.get("/api/companies")
async def list_companies(
    search: str = "",
    owner_id: int = 0,
    unassigned: int = 0,
    tag: str = "",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List active companies with denormalized counts (no sites loaded)."""

    @cached_endpoint(
        prefix="company_list", ttl_hours=0.5, key_params=["search", "owner_id", "unassigned", "tag", "limit", "offset"]
    )
    def _fetch(search, owner_id, unassigned, tag, limit, offset, db):
        query = (
            db.query(Company)
            .filter(Company.is_active == True)  # noqa: E712
            .options(joinedload(Company.account_owner))
        )
        if search.strip():
            safe = escape_like(search.strip())
            query = query.filter(Company.name.ilike(f"%{safe}%"))
        if tag.strip():
            safe_tag = tag.strip().lower()
            query = query.filter(
                sqlfunc.lower(sqlfunc.cast(Company.brand_tags, String)).contains(safe_tag)
                | sqlfunc.lower(sqlfunc.cast(Company.commodity_tags, String)).contains(safe_tag)
            )
        if unassigned:
            query = query.filter(Company.account_owner_id == None)  # noqa: E711
        if owner_id and not unassigned:
            # Server-side owner filter: only companies with at least one site owned by this user
            query = query.filter(
                Company.id.in_(
                    db.query(CustomerSite.company_id).filter(
                        CustomerSite.owner_id == owner_id,
                        CustomerSite.is_active == True,  # noqa: E712
                    )
                )
            )
        query = query.order_by(Company.name)
        total = query.count()
        companies = query.offset(offset).limit(limit).all()

        # Compute per-company win_rate and last_req_date
        company_ids = [c.id for c in companies]
        stats_map: dict[int, dict] = {}
        if company_ids:
            from sqlalchemy import case as sa_case

            stats_rows = (
                db.query(
                    CustomerSite.company_id,
                    sqlfunc.count(sa_case((Requisition.status == "won", 1))).label("won_count"),
                    sqlfunc.count(sa_case((Requisition.status.in_(["won", "lost"]), 1))).label("decided_count"),
                    sqlfunc.max(Requisition.created_at).label("last_req_date"),
                )
                .join(Requisition, Requisition.customer_site_id == CustomerSite.id)
                .filter(CustomerSite.company_id.in_(company_ids))
                .group_by(CustomerSite.company_id)
                .all()
            )
            for row in stats_rows:
                wr = round(row.won_count / row.decided_count * 100) if row.decided_count > 0 else None
                stats_map[row.company_id] = {
                    "win_rate": wr,
                    "last_req_date": row.last_req_date.isoformat() if row.last_req_date else None,
                }

        # 90-day won revenue per company
        from datetime import datetime as _dt
        from datetime import timedelta, timezone as _tz

        rev_cutoff = _dt.now(_tz.utc) - timedelta(days=90)
        revenue_map: dict[int, float] = {}
        if company_ids:
            rev_rows = (
                db.query(
                    CustomerSite.company_id,
                    sqlfunc.coalesce(sqlfunc.sum(Quote.subtotal), 0).label("revenue"),
                )
                .join(Requisition, Requisition.customer_site_id == CustomerSite.id)
                .join(Quote, Quote.requisition_id == Requisition.id)
                .filter(
                    CustomerSite.company_id.in_(company_ids),
                    Requisition.status == "won",
                    Quote.created_at >= rev_cutoff,
                )
                .group_by(CustomerSite.company_id)
                .all()
            )
            for row in rev_rows:
                revenue_map[row.company_id] = float(row.revenue)

        items = []
        for c in companies:
            st = stats_map.get(c.id, {})
            items.append(
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
                    "last_enriched_at": c.last_enriched_at.isoformat() if c.last_enriched_at else None,
                    "enrichment_source": c.enrichment_source,
                    "account_type": c.account_type,
                    "phone": c.phone,
                    "credit_terms": c.credit_terms,
                    "tax_id": c.tax_id,
                    "currency": c.currency,
                    "preferred_carrier": c.preferred_carrier,
                    "is_strategic": c.is_strategic,
                    "brand_tags": c.brand_tags or [],
                    "commodity_tags": c.commodity_tags or [],
                    "account_owner_id": c.account_owner_id,
                    "account_owner_name": (c.account_owner.name if c.account_owner else None)
                    if c.account_owner_id
                    else None,
                    "customer_enrichment_status": c.customer_enrichment_status,
                    "customer_enrichment_at": c.customer_enrichment_at.isoformat()
                    if c.customer_enrichment_at
                    else None,
                    "site_count": c.site_count or 0,
                    "open_req_count": c.open_req_count or 0,
                    "win_rate": st.get("win_rate"),
                    "last_req_date": st.get("last_req_date"),
                    "revenue_90d": revenue_map.get(c.id, 0),
                }
            )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    return await asyncio.to_thread(
        _fetch,
        search=search,
        owner_id=owner_id,
        unassigned=unassigned,
        tag=tag,
        limit=limit,
        offset=offset,
        db=db,
    )


@router.get("/api/companies/typeahead")
async def companies_typeahead(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lightweight endpoint returning all active companies + site IDs for the
    requisition creation typeahead. No limit, minimal payload.
    Cached for 2 hours — invalidated when companies/sites change."""

    @cached_endpoint(prefix="companies_typeahead", ttl_hours=2, key_params=[])
    def _fetch(db):
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
                "sites": [{"id": s.id, "site_name": s.site_name} for s in c.sites if s.is_active],
            }
            for c in companies
        ]

    return _fetch(db=db)


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


@router.get("/api/companies/{company_id}")
async def get_company(
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Fetch a single company with sites, contacts, and open req counts."""
    company = (
        db.query(Company)
        .filter(Company.id == company_id)
        .options(
            selectinload(Company.sites).joinedload(CustomerSite.owner),
            selectinload(Company.sites).selectinload(CustomerSite.site_contacts),
            joinedload(Company.account_owner),
        )
        .first()
    )
    if not company:
        raise HTTPException(404, "Company not found")

    @cached_endpoint(prefix="company_detail", ttl_hours=1, key_params=["company_id"])
    def _fetch(company_id, db):
        # Open req counts per site
        site_ids = [s.id for s in company.sites if s.is_active]
        site_open_counts: dict[int, int] = {}
        if site_ids:
            count_rows = (
                db.query(
                    Requisition.customer_site_id,
                    sqlfunc.count(Requisition.id),
                )
                .filter(
                    Requisition.status.notin_(["archived", "won", "lost"]),
                    Requisition.customer_site_id.in_(site_ids),
                )
                .group_by(Requisition.customer_site_id)
                .all()
            )
            site_open_counts = {row[0]: row[1] for row in count_rows}

        sites = []
        for s in company.sites:
            if not s.is_active:
                continue
            open_count = site_open_counts.get(s.id, 0)
            contacts = [
                {
                    "id": sc.id,
                    "full_name": sc.full_name,
                    "title": sc.title,
                    "email": sc.email,
                    "phone": sc.phone,
                    "is_primary": sc.is_primary,
                    "is_active": sc.is_active,
                }
                for sc in s.site_contacts
            ]
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
                    "contacts": contacts,
                }
            )
        return {
            "id": company.id,
            "name": company.name,
            "website": company.website,
            "industry": company.industry,
            "notes": company.notes,
            "domain": company.domain,
            "linkedin_url": company.linkedin_url,
            "legal_name": company.legal_name,
            "employee_size": company.employee_size,
            "hq_city": company.hq_city,
            "hq_state": company.hq_state,
            "hq_country": company.hq_country,
            "last_enriched_at": company.last_enriched_at.isoformat() if company.last_enriched_at else None,
            "enrichment_source": company.enrichment_source,
            "account_type": company.account_type,
            "phone": company.phone,
            "credit_terms": company.credit_terms,
            "tax_id": company.tax_id,
            "currency": company.currency,
            "preferred_carrier": company.preferred_carrier,
            "is_strategic": company.is_strategic,
            "brand_tags": company.brand_tags or [],
            "commodity_tags": company.commodity_tags or [],
            "tags": _load_company_tags(company.id, db),
            "account_owner_id": company.account_owner_id,
            "account_owner_name": (company.account_owner.name if company.account_owner else None)
            if company.account_owner_id
            else None,
            "source": company.source,
            "created_at": company.created_at.isoformat() if company.created_at else None,
            "updated_at": company.updated_at.isoformat() if company.updated_at else None,
            "site_count": len(sites),
            "sites": sites,
        }

    return _fetch(company_id=company_id, db=db)


@router.post("/api/companies")
async def create_company(
    payload: CompanyCreate,
    force: bool = Query(False),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new company with auto-enrichment and default HQ site.

    When force=False (default), checks for duplicate companies first.
    Returns 409 with duplicate list if near-matches are found.
    Pass force=True to skip the duplicate check and create anyway.
    """
    from ...enrichment_service import normalize_company_input

    clean_name, clean_domain = await normalize_company_input(payload.name, payload.domain or "")

    # Duplicate check (unless force=True)
    if not force:
        _suffixes = re.compile(
            r"\b(inc\.?|llc\.?|ltd\.?|corp\.?|co\.?|plc\.?|gmbh|ag|sa|s\.?a\.?|"
            r"s\.?r\.?l\.?|pty\.?|b\.?v\.?|n\.?v\.?|a\.?s\.?|oy|ab|limited|"
            r"corporation|incorporated|company)\s*$",
            re.IGNORECASE,
        )

        def _norm(n: str) -> str:
            n = n.strip().lower()
            n = re.sub(r"[^\w\s]", " ", n)
            n = _suffixes.sub("", n).strip()
            return re.sub(r"\s+", " ", n)

        query_clean = _norm(clean_name)
        if query_clean:
            companies = (
                db.query(Company.id, Company.name)
                .filter(Company.is_active == True)  # noqa: E712
                .limit(2000)
                .all()
            )
            duplicates = []
            for c in companies:
                cn = _norm(c.name)
                if not cn:
                    continue
                if cn == query_clean:
                    duplicates.append({"id": c.id, "name": c.name, "match": "exact"})
                elif cn in query_clean or query_clean in cn:  # pragma: no cover
                    duplicates.append({"id": c.id, "name": c.name, "match": "similar"})
                elif len(query_clean) >= 6 and len(cn) >= 6 and cn[:6] == query_clean[:6]:  # pragma: no cover
                    duplicates.append({"id": c.id, "name": c.name, "match": "similar"})
            if duplicates:
                return JSONResponse(
                    status_code=409,
                    content={"duplicates": duplicates[:5]},
                )
    # Extract domain from website if no explicit domain
    if not clean_domain and payload.website:
        clean_domain = (
            payload.website.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].lower()
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

    # Capture response values eagerly so background tasks can't detach them
    result = {
        "id": company.id,
        "name": company.name,
        "default_site_id": default_site.id,
        "enrich_triggered": False,
    }

    # Auto-enrich if domain is available
    domain = company.domain or ""
    if domain and (
        get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
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

                    # Run customer enrichment waterfall for immediate contact discovery
                    if settings.customer_enrichment_enabled:  # pragma: no cover
                        try:
                            from ...services.customer_enrichment_service import enrich_customer_account

                            waterfall = await enrich_customer_account(cid, s, force=True)
                            if waterfall.get("contacts_added", 0) > 0:
                                s.commit()
                                logger.info(
                                    "Auto-enriched company %d: %d contacts via waterfall",
                                    cid,
                                    waterfall["contacts_added"],
                                )
                        except Exception as we:
                            logger.warning("Waterfall auto-enrich error for company %d: %s", cid, we)
                            s.rollback()
                finally:
                    s.close()
            except Exception:
                logger.exception("Background enrichment failed for company %d", cid)

        asyncio.create_task(_enrich_company_bg(result["id"], result["default_site_id"], domain, result["name"]))
        result["enrich_triggered"] = True

    invalidate_prefix("company_list")
    invalidate_prefix("companies_typeahead")
    return result


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
    invalidate_prefix("company_list")
    invalidate_prefix("companies_typeahead")
    invalidate_prefix("company_detail")
    return {"ok": True}


@router.post("/api/companies/{company_id}/summarize")
async def summarize_company(
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate an AI-powered strategic account summary."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    from ...services.account_summary_service import generate_account_summary

    result = await generate_account_summary(company_id, db)
    if not result:
        return {"situation": "", "development": "", "next_steps": []}
    return result


@router.post("/api/companies/{company_id}/analyze-tags")
async def analyze_company_tags(
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger AI analysis of customer's requisition history to generate brand/commodity tags."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    from ...services.customer_analysis_service import analyze_customer_materials

    await analyze_customer_materials(company_id, db_session=db)
    db.refresh(company)
    invalidate_prefix("company_detail")
    return {
        "ok": True,
        "brand_tags": company.brand_tags or [],
        "commodity_tags": company.commodity_tags or [],
    }
