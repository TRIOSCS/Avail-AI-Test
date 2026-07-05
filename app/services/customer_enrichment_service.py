"""Customer Enrichment Service — contact enrichment for customer accounts.

Enriches customer accounts with verified contacts.
Utility functions (contact classification, dedup, gap detection) are
available for enrichment providers.

Priority: Assigned accounts first, then unassigned accounts.

Called by: enrichment router endpoints, batch scheduler, the CRM auto-enrich background
           task (app/routers/crm/companies.py).
Depends on: enrichment_service.find_suggested_contacts_with_errors (the live multi-provider
            contact-discovery waterfall), contact_quality.py.
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..models.crm import Company, CustomerSite, SiteContact

# Title keywords for classifying contact roles
_BUYER_TITLES = {"buyer", "purchasing", "procurement", "sourcing", "supply chain", "commodity"}
_TECHNICAL_TITLES = {"engineer", "engineering", "technical", "design", "r&d", "quality"}
_DECISION_TITLES = {
    "director",
    "vp",
    "vice president",
    "president",
    "ceo",
    "cfo",
    "coo",
    "owner",
    "gm",
    "general manager",
    "chief",
}


def _classify_contact_role(title: str | None) -> str:
    """Classify a contact's role from their job title."""
    if not title:
        return "unknown"
    t = title.lower()
    if any(kw in t for kw in _DECISION_TITLES):
        return "decision_maker"
    if any(kw in t for kw in _BUYER_TITLES):
        return "buyer"
    if any(kw in t for kw in _TECHNICAL_TITLES):
        return "technical"
    return "operations"


def _is_direct_dial(phone_type: str | None) -> bool:
    """Check if a phone is a direct dial (not switchboard)."""
    return phone_type in ("direct_dial", "mobile")


def _contacts_needed(db: Session, company_id: int, target: int) -> int:
    """Count how many more contacts are needed for a company."""
    sites = db.query(CustomerSite.id).filter_by(company_id=company_id).all()
    if not sites:
        return target
    site_ids = [s.id for s in sites]
    existing = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id.in_(site_ids), SiteContact.is_active == True)  # noqa: E712
        .count()
    )
    return max(0, target - existing)


def _get_company_domain(company: Company) -> str | None:
    """Extract clean domain from company."""
    domain = company.domain or company.website or ""
    if domain:
        domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].strip()
    return domain or None


def _resolve_target_site(db: Session, company: Company) -> CustomerSite:
    """Pick the site to attach discovered contacts to (HQ → first active → any → new).

    Mirrors the "Find contacts" Add flow's site resolution (contacts_tab_add_suggested):
    prefer an HQ site, else the first active site, else any site. If the company somehow
    has no site at all, create an HQ site so discovered contacts always have a home.
    """
    sites = db.query(CustomerSite).filter(CustomerSite.company_id == company.id).all()
    if sites:
        pool = [s for s in sites if s.is_active] or sites
        hq = next((s for s in pool if (s.site_type or "").lower() == "hq"), None)
        return hq or pool[0]
    site = CustomerSite(company_id=company.id, site_name="HQ", site_type="hq", is_active=True)
    db.add(site)
    db.flush()
    return site


def _persist_discovered_contacts(db: Session, company: Company, contacts: list[dict]) -> tuple[int, int]:
    """Persist blended discovery results as SiteContact rows; return (added, verified).

    Dedupes per-site exactly like the manual "Add" path (contacts_tab_add_suggested):
    case-insensitive email, or case-insensitive full_name when the email is absent. Tags
    ``enrichment_source`` (provenance) and ``contact_role`` (roster classification).
    """
    if not contacts:
        return 0, 0

    site = _resolve_target_site(db, company)
    existing = db.query(SiteContact).filter(SiteContact.customer_site_id == site.id).all()
    seen_emails = {c.email.lower() for c in existing if c.email}
    seen_names = {c.full_name.lower() for c in existing if c.full_name and not c.email}

    added = verified = 0
    for c in contacts:
        full_name = (c.get("full_name") or "").strip()
        email = (c.get("email") or "").strip().lower() or None
        if not full_name and not email:
            continue
        if email:
            if email in seen_emails:
                continue
        elif full_name.lower() in seen_names:
            continue

        is_verified = bool(c.get("verified"))
        db.add(
            SiteContact(
                customer_site_id=site.id,
                full_name=full_name or "Unknown",
                title=(c.get("title") or "").strip() or None,
                email=email,
                phone=(c.get("phone") or "").strip() or None,
                linkedin_url=(c.get("linkedin_url") or "").strip() or None,
                enrichment_source=(c.get("source") or "").strip() or "enrichment",
                email_verified=is_verified,
                contact_role=_classify_contact_role(c.get("title")),
            )
        )
        added += 1
        verified += int(is_verified)
        if email:
            seen_emails.add(email)
        elif full_name:
            seen_names.add(full_name.lower())
    return added, verified


async def enrich_customer_account(
    company_id: int,
    db: Session,
    force: bool = False,
) -> dict:
    """Run contact enrichment for a customer account.

    Drives the live multi-provider contact-discovery waterfall — the SAME path the async
    "Find contacts" button uses (``find_suggested_contacts_with_errors`` →
    ``enrichment_router.gather_contacts`` → blend → relevance filter) — and PERSISTS the
    discovered contacts as SiteContact rows (deduped per-site). Preserves graceful
    degraded-provider handling: providers that error are surfaced in ``errored_providers``
    and a total failure never crashes the caller (it degrades to zero contacts).
    """
    if not settings.customer_enrichment_enabled:
        return {"error": "Customer enrichment is disabled", "contacts_added": 0}

    company = db.get(Company, company_id)
    if not company:
        return {"error": "Company not found", "contacts_added": 0}

    # Cooldown check
    if not force and company.customer_enrichment_at:
        cooldown = timedelta(days=settings.customer_enrichment_cooldown_days)
        next_enrichment_at = company.customer_enrichment_at + cooldown
        now = datetime.now(timezone.utc)
        if now < next_enrichment_at:
            days_left = (next_enrichment_at - now).days
            return {
                "error": f"Cooldown active — {days_left} days remaining",
                "contacts_added": 0,
                "next_enrichment_at": next_enrichment_at.isoformat(),
            }

    domain = _get_company_domain(company)
    if not domain:
        return {"error": "No domain available", "contacts_added": 0}

    target = settings.customer_enrichment_contacts_per_account
    needed = _contacts_needed(db, company_id, target)
    if needed <= 0 and not force:
        company.customer_enrichment_status = "complete"
        company.customer_enrichment_at = datetime.now(timezone.utc)
        db.flush()
        return {"ok": True, "contacts_added": 0, "status": "already_complete"}

    limit = needed if needed > 0 else target

    # Live contact-discovery waterfall (Hunter/Clay/Lusha/Explorium + AI). Lazy import to
    # keep the enrichment_service ↔ customer_enrichment_service dependency one-directional.
    from ..enrichment_service import find_suggested_contacts_with_errors

    try:
        contacts, errored_providers = await find_suggested_contacts_with_errors(domain, company.name or "", limit=limit)
    except Exception:
        # Graceful degradation: fold a total discovery failure into the degraded banner
        # (mirrors _run_contact_discovery) — never crash the auto-enrich background task.
        logger.exception("Customer enrichment discovery failed for {}", company.name)
        contacts, errored_providers = [], ["all"]

    added, verified = _persist_discovered_contacts(db, company, contacts)

    company.customer_enrichment_at = datetime.now(timezone.utc)
    remaining = _contacts_needed(db, company_id, target)
    company.customer_enrichment_status = "complete" if remaining <= 0 else "partial"
    db.flush()

    if added == 0 and errored_providers:
        run_status = "degraded"
    elif remaining <= 0:
        run_status = "complete"
    else:
        run_status = "partial"

    sources_used = sorted({s for c in contacts for s in (c.get("source") or "").split("+") if s})
    logger.info(
        "Customer enrichment for {}: added {} contacts ({} verified); errored={}",
        company.name,
        added,
        verified,
        errored_providers,
    )
    return {
        "ok": True,
        "company_id": company_id,
        "contacts_added": added,
        "contacts_verified": verified,
        "sources_used": sources_used,
        "errored_providers": errored_providers,
        "status": run_status,
    }


def get_enrichment_gaps(db: Session, limit: int = 50) -> list[dict]:
    """Find customer accounts that need enrichment, prioritizing assigned accounts.

    Returns companies sorted by: assigned first, then by last activity.
    """
    target = settings.customer_enrichment_contacts_per_account
    cooldown = timedelta(days=settings.customer_enrichment_cooldown_days)
    cutoff = datetime.now(timezone.utc) - cooldown

    companies = (
        db.query(Company)
        .filter(
            Company.is_active == True,  # noqa: E712
            (Company.customer_enrichment_at.is_(None)) | (Company.customer_enrichment_at < cutoff),
        )
        .order_by(
            # Assigned accounts FIRST (account_owner_id IS NOT NULL first)
            Company.account_owner_id.is_(None).asc(),
            Company.last_activity_at.desc().nullslast(),
        )
        .limit(limit)
        .all()
    )

    gaps = []
    for co in companies:
        needed = _contacts_needed(db, co.id, target)
        if needed > 0:
            gaps.append(
                {
                    "company_id": co.id,
                    "company_name": co.name,
                    "domain": _get_company_domain(co),
                    "account_owner_id": co.account_owner_id,
                    "contacts_needed": needed,
                    "current_status": co.customer_enrichment_status,
                    "last_enriched": co.customer_enrichment_at.isoformat() if co.customer_enrichment_at else None,
                }
            )
    return gaps
