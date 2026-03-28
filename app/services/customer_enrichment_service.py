"""Customer Enrichment Service — contact enrichment for customer accounts.

Enriches customer accounts with verified contacts.
Utility functions (contact classification, dedup, gap detection) are
available for enrichment providers.

Priority: Assigned accounts first, then unassigned accounts.

Called by: enrichment router endpoints, batch scheduler.
Depends on: credit_manager.py, contact_quality.py.
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


async def enrich_customer_account(
    company_id: int,
    db: Session,
    force: bool = False,
) -> dict:
    """Run contact enrichment for a customer account.

    Previously used Apollo, Hunter, and Lusha connectors (now removed). Returns a stub
    result until new enrichment providers are configured.
    """
    if not settings.customer_enrichment_enabled:
        return {"error": "Customer enrichment is disabled", "contacts_added": 0}

    company = db.get(Company, company_id)
    if not company:
        return {"error": "Company not found", "contacts_added": 0}

    # Cooldown check
    if not force and company.customer_enrichment_at:
        cooldown = timedelta(days=settings.customer_enrichment_cooldown_days)
        if datetime.now(timezone.utc) - company.customer_enrichment_at < cooldown:
            days_left = (company.customer_enrichment_at + cooldown - datetime.now(timezone.utc)).days
            return {
                "error": f"Cooldown active — {days_left} days remaining",
                "contacts_added": 0,
                "next_enrichment_at": (company.customer_enrichment_at + cooldown).isoformat(),
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

    # No enrichment providers currently configured (Apollo/Hunter/Lusha removed)
    logger.info(
        "Customer enrichment for %s: no providers configured, skipping",
        company.name,
    )
    return {
        "ok": True,
        "company_id": company_id,
        "contacts_added": 0,
        "contacts_verified": 0,
        "sources_used": [],
        "status": "no_providers",
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
