import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import can_manage_account, require_admin, require_buyer, require_user
from ...dependencies import is_admin as _is_admin
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


async def _run_company_enrichment(company_id: int, domain: str, name: str) -> None:
    """Background worker: run the account enrichment waterfall for one company.

    Scheduled by ``enrich_company`` (HTMX path) so the click never blocks on the ~20-40s of
    external-provider calls: firmographics (``enrich_entity``: SAM.gov + Clay/Explorium/Lusha
    + Anthropic) then contact discovery (``find_suggested_contacts_with_errors``: Hunter/Clay).
    Firmographics are committed here; the transient result (which fields changed, discovered
    contacts, errored providers) is recorded in ``company_enrich_runs`` so the enrich-status
    poller can render the same ``_enrich_result.html`` panel the synchronous path produced.

    A firmographics failure marks the outcome ``blocked`` (the poller shows a "couldn't
    complete" toast). A contact-discovery hiccup is NOT blocked — it degrades to the amber
    "couldn't reach" banner via ``errored_providers``, mirroring the old inline behavior.

    Opens its own session — FastAPI has already returned the response and closed the request
    session by the time this runs. Must NEVER raise: it is a fire-and-forget task.
    """
    from ...database import SessionLocal
    from ...services.company_enrich_runs import CompanyEnrichOutcome, company_enrich_runs

    db = SessionLocal()
    blocked = False
    updated: list[str] = []
    suggested: list[dict] = []
    errored: list[str] = []
    company_missing = False
    try:
        company = db.get(Company, company_id)
        if company is None:
            # Company vanished between click and run — nothing to enrich; drop the guard.
            company_missing = True
        else:
            # Firmographics — commit on success; a genuine outage marks the run blocked (toast).
            try:
                from ...enrichment_service import apply_enrichment_to_company, enrich_entity

                enrichment = await enrich_entity(domain, name)
                updated = apply_enrichment_to_company(company, enrichment)
                db.commit()
            except Exception as e:
                logger.opt(exception=e).warning("Account enrichment firmographics failed for {}", company_id)
                db.rollback()
                blocked = True

            # Contact discovery — degrade to the amber "couldn't reach" banner, never a toast.
            try:
                from ...enrichment_service import find_suggested_contacts_with_errors

                suggested, errored = await find_suggested_contacts_with_errors(domain, name)
            except Exception as e:
                logger.opt(exception=e).warning("Account enrichment contact discovery failed for {}", company_id)
                errored = ["all"]
    except Exception:
        logger.exception("Account enrichment task crashed for {}", company_id)
        blocked = True
    finally:
        db.close()
        if company_missing:
            company_enrich_runs.clear(company_id)
        else:
            company_enrich_runs.finish(
                company_id,
                CompanyEnrichOutcome(
                    blocked=blocked,
                    updated_fields=updated,
                    suggested=suggested,
                    errored_providers=errored,
                ),
            )


def _resolve_company_domain(company: Company, payload: EnrichDomainRequest) -> str:
    """Domain to enrich: explicit payload override, else the company's domain/website."""
    domain = company.domain or company.website or ""
    if domain:
        domain = _normalize_domain(domain)
    if payload.domain:
        domain = payload.domain
    return domain


@router.post("/api/enrich/company/{company_id}")
async def enrich_company(
    company_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    payload: EnrichDomainRequest = EnrichDomainRequest(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Enrich a customer company with external data.

    Content-negotiates on the HX-Request header:

      * HTMX callers (the Enrich button) **enqueue** the provider waterfall on a background
        task and return an "Enriching…" poller immediately — the click never blocks on the
        ~20-40s of SAM.gov/Clay/Explorium/Lusha/Anthropic/Hunter calls. When the run lands,
        the poller swaps in the firmographics + discovered-contacts panel (or a "couldn't
        complete" toast if a data source was down).
      * Programmatic callers get the synchronous JSON result (firmographics awaited inline).
    """
    _require_enrichment_provider()

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "You do not have access to this company")
    domain = _resolve_company_domain(company, payload)
    if not domain:
        raise HTTPException(400, "No domain available — set company website or domain first")

    if _wants_html(request):
        # Async path: schedule the waterfall and return the polling "Enriching…" panel.
        from ...services.company_enrich_runs import company_enrich_runs

        if company_enrich_runs.begin(company_id):
            background_tasks.add_task(_run_company_enrichment, company_id, domain, company.name or "")
        return template_response(
            "htmx/partials/customers/enrich_status.html",
            {"request": request, "company": company},
        )

    # JSON/programmatic path: synchronous firmographics (unchanged contract).
    from ...enrichment_service import apply_enrichment_to_company, enrich_entity

    enrichment = await enrich_entity(domain, company.name)
    updated = apply_enrichment_to_company(company, enrichment)
    db.commit()
    return {"ok": True, "updated_fields": updated, "enrichment": enrichment}


@router.get("/api/enrich/company/{company_id}/status")
async def enrich_company_status(
    company_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Poll target for the account "Enriching…" panel — reflects the background run's
    state.

    While the run is in flight, re-renders the polling panel (keep polling). When it lands,
    returns the ``_enrich_result.html`` panel (firmographics + discovered contacts) and
    answers HTTP 286 to STOP polling; a blocked run (a data source was unavailable) also
    fires the "couldn't complete" toast. A deleted company or an already-consumed outcome
    stops polling with an empty body.
    """
    from ...services.company_enrich_runs import company_enrich_runs

    company = db.get(Company, company_id)
    if not company:
        # Polling sub-resource: htmx neither swaps nor cancels an `every 2s` poll on a 4xx,
        # so a 404 would leave the panel hammering this route. 286 stops it; empty clears it.
        return HTMLResponse("", status_code=286)
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "You do not have access to this company")

    if company_enrich_runs.is_running(company_id):
        return template_response(
            "htmx/partials/customers/enrich_status.html",
            {"request": request, "company": company},
        )

    outcome = company_enrich_runs.consume_outcome(company_id)
    if outcome is None:
        # No run in flight and no pending outcome (already consumed, or lost on restart) —
        # stop polling and clear the panel.
        return HTMLResponse("", status_code=286)

    active_sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )
    response = template_response(
        "htmx/partials/shared/_enrich_result.html",
        {
            "request": request,
            "entity": company,
            "entity_type": "company",
            "updated_fields": outcome.updated_fields,
            "show_contacts": True,
            "suggested": outcome.suggested,
            "errored_providers": outcome.errored_providers,
            "active_sites": active_sites,
            "add_target": "closest li",
            "add_swap": "outerHTML",
        },
    )
    response.status_code = 286  # htmx's stop-polling status — the result panel still swaps in.
    if outcome.blocked:
        # Bridged to the global $store.toast via the showToast HX-Trigger convention.
        response.headers["HX-Trigger"] = json.dumps(
            {
                "showToast": {
                    "message": "Enrichment couldn't complete — a data source was unavailable. Try again shortly.",
                    "type": "error",
                }
            }
        )
    return response


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
    company = db.get(Company, site.company_id)
    if not company or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
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
