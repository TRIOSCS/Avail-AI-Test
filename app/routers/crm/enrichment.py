from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_admin, require_buyer, require_user
from ...models import Company, CustomerSite, SiteContact, SyncLog, User, VendorCard, VendorContact
from ...rate_limit import limiter
from ...schemas.crm import AddContactsToVendor, AddContactToSite, CustomerImportRow, EnrichDomainRequest
from ...services.credential_service import get_credential_cached
from ...template_env import template_response

router = APIRouter()


def _normalize_domain(value: str) -> str:
    """Strip scheme, www, and any path from a domain/website string."""
    return value.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]


def _wants_html(request: Request) -> bool:
    """True for HTMX requests — the enrich button POSTs with the HX-Request header.

    HTMX callers get a rendered result panel; programmatic/API callers get JSON.
    """
    return request.headers.get("HX-Request") == "true"


def _require_enrichment_provider() -> None:
    """Raise 503 unless at least one enrichment provider credential is configured."""
    has_provider = (
        get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        or get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
        or get_credential_cached("hunter_enrichment", "HUNTER_API_KEY")
    )
    if not has_provider:
        raise HTTPException(
            503,
            "No enrichment providers configured — set EXPLORIUM_API_KEY, ANTHROPIC_API_KEY, or HUNTER_API_KEY in .env",
        )


# ── Enrichment (shared for vendors + customers) ─────────────────────────


@router.post("/api/enrich/company/{company_id}")
async def enrich_company(
    company_id: int,
    request: Request,
    payload: EnrichDomainRequest = EnrichDomainRequest(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Enrich a customer company with external data.

    Content-negotiates on the HX-Request header: HTMX callers (the Enrich button) get a
    rendered result panel — firmographics found, what was updated vs already-current, and
    discovered contacts with Add buttons; programmatic callers get JSON.
    """
    _require_enrichment_provider()
    from ...enrichment_service import apply_enrichment_to_company, enrich_entity

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    domain = company.domain or company.website or ""
    if domain:
        domain = _normalize_domain(domain)
    if payload.domain:
        domain = payload.domain
    if not domain:
        raise HTTPException(400, "No domain available — set company website or domain first")
    enrichment = await enrich_entity(domain, company.name)
    updated = apply_enrichment_to_company(company, enrichment)
    db.commit()

    if not _wants_html(request):
        return {"ok": True, "updated_fields": updated, "enrichment": enrichment}

    # HTMX path: discover contacts for the panel via the real Hunter/Clay waterfall.
    # Firmographics are already committed, so a discovery failure degrades to an amber
    # banner rather than blocking the panel.
    suggested: list[dict] = []
    errored: list[str] = []
    try:
        from ...enrichment_service import find_suggested_contacts_with_errors

        suggested, errored = await find_suggested_contacts_with_errors(domain, company.name or "")
    except Exception as e:
        # Degrade to the amber "couldn't reach" banner rather than 500 the panel, but keep
        # the traceback (exc_info) so a genuine bug here still reaches Sentry/logs.
        logger.opt(exception=e).warning("enrich_company contact discovery failed for {}", company_id)
        errored = ["all"]

    active_sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )
    return template_response(
        "htmx/partials/shared/_enrich_result.html",
        {
            "request": request,
            "entity": company,
            "entity_type": "company",
            "updated_fields": updated,
            "show_contacts": True,
            "suggested": suggested,
            "errored_providers": errored,
            "active_sites": active_sites,
            "add_target": "closest li",
            "add_swap": "outerHTML",
        },
    )


@router.post("/api/enrich/vendor/{card_id}")
async def enrich_vendor_card(
    card_id: int,
    request: Request,
    payload: EnrichDomainRequest = EnrichDomainRequest(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Enrich a vendor card with external data.

    Content-negotiates on HX-Request: HTMX callers get the firmographics result panel
    (contact discovery is company-only for now); programmatic callers get JSON.
    """
    _require_enrichment_provider()
    from ...enrichment_service import apply_enrichment_to_vendor, enrich_entity

    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor card not found")
    domain = card.domain or card.website or ""
    if domain:
        domain = _normalize_domain(domain)
    if payload.domain:
        domain = payload.domain
    if not domain:
        raise HTTPException(400, "No domain available — set vendor website or domain first")
    enrichment = await enrich_entity(domain, card.display_name)
    updated = apply_enrichment_to_vendor(card, enrichment)
    db.commit()

    if not _wants_html(request):
        return {"ok": True, "updated_fields": updated, "enrichment": enrichment}

    return template_response(
        "htmx/partials/shared/_enrich_result.html",
        {
            "request": request,
            "entity": card,
            "entity_type": "vendor",
            "updated_fields": updated,
            "show_contacts": False,
        },
    )


@router.get("/api/suggested-contacts")
async def get_suggested_contacts(
    domain: str = "",
    name: str = "",
    title: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Find suggested contacts at a company from enrichment providers."""
    _require_enrichment_provider()
    from ...enrichment_service import find_suggested_contacts

    if not domain:
        raise HTTPException(400, "domain parameter is required")
    domain = _normalize_domain(domain)
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
    """Add a suggested contact as a real SiteContact on a customer site.

    Mirrors add_suggested_to_vendor: resolves the site (404 if missing),
    deduplicates per-site by case-insensitive email (or by case-insensitive
    full_name when email is absent), and creates a SiteContact with
    enrichment_source tagged for provenance.

    WARNING: this is a JSON-only endpoint. Do NOT call it from HTMX with
    hx-target/hx-swap — the Contacts-tab flow uses the HTML endpoint at
    /v2/partials/customers/{company_id}/suggested-contacts/add instead.
    """
    site = db.get(CustomerSite, payload.site_id)
    if not site:
        raise HTTPException(404, "Customer site not found")
    c = payload.contact
    # Email-based dedup (case-insensitive, mirrors create_site_contact :6003)
    if c.email:
        existing = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == payload.site_id,
                sqlfunc.lower(SiteContact.email) == c.email,  # already lowercased by schema validator
            )
            .first()
        )
        if existing:
            logger.info(
                "add_suggested_to_site: dedup by email for site_id={} email={}",
                payload.site_id,
                c.email,
            )
            return {"ok": True, "added": 0, "deduped": True}
    else:
        # Null-email dedup: case-insensitive full_name within the site
        # (without this, PG allows multiple NULL-email rows — silent dupes)
        if c.full_name:
            existing_name = (
                db.query(SiteContact)
                .filter(
                    SiteContact.customer_site_id == payload.site_id,
                    SiteContact.email.is_(None),
                    sqlfunc.lower(SiteContact.full_name) == c.full_name.strip().lower(),
                )
                .first()
            )
            if existing_name:
                logger.info(
                    "add_suggested_to_site: dedup by name for site_id={} name={}",
                    payload.site_id,
                    c.full_name,
                )
                return {"ok": True, "added": 0, "deduped": True}

    sc = SiteContact(
        customer_site_id=payload.site_id,
        full_name=(c.full_name or "").strip() or "Unknown",
        title=c.title,
        email=c.email or None,
        phone=c.phone,
        linkedin_url=c.linkedin_url,
        enrichment_source=c.source or "enrichment",
        email_verified=c.email_verified,
    )
    db.add(sc)
    db.commit()
    db.refresh(sc)
    logger.info(
        "add_suggested_to_site: created SiteContact id={} for site_id={}",
        sc.id,
        payload.site_id,
    )
    return {"ok": True, "added": 1, "contact_id": sc.id}


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
