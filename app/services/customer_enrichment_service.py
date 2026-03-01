"""Customer Enrichment Service — waterfall: Apollo → Hunter → Lusha phones → Other.

Enriches customer accounts with verified contacts. Apollo is the primary
contact discovery source (names, emails, titles). Hunter verifies emails.
Lusha is reserved for phone enrichment only (direct dials, mobiles) to
conserve expensive credits. Credit-budget-aware, respects cooldown
periods, and enforces data quality (phone-verified dials, verified emails).

Priority: Assigned accounts first, then unassigned accounts.

Called by: enrichment router endpoints, batch scheduler.
Depends on: lusha_client, hunter_client, apollo_client, credit_manager.
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..models.crm import Company, CustomerSite, SiteContact
from .credit_manager import can_use_credits, record_credit_usage

# Title keywords for classifying contact roles
_BUYER_TITLES = {"buyer", "purchasing", "procurement", "sourcing", "supply chain", "commodity"}
_TECHNICAL_TITLES = {"engineer", "engineering", "technical", "design", "r&d", "quality"}
_DECISION_TITLES = {"director", "vp", "vice president", "president", "ceo", "cfo", "coo", "owner", "gm", "general manager", "chief"}


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
        domain = (
            domain.replace("https://", "")
            .replace("http://", "")
            .replace("www.", "")
            .split("/")[0]
            .strip()
        )
    return domain or None


def _ensure_site(db: Session, company: Company) -> CustomerSite:
    """Get or create a default site for the company."""
    site = db.query(CustomerSite).filter_by(company_id=company.id).first()
    if not site:
        site = CustomerSite(
            company_id=company.id,
            site_name=f"{company.name} - HQ",
        )
        db.add(site)
        db.flush()
    return site


def _dedup_contacts(contacts: list[dict]) -> list[dict]:
    """Deduplicate contacts by email, keeping the first (higher-priority) source."""
    seen_emails = set()
    result = []
    for c in contacts:
        email = (c.get("email") or "").lower().strip()
        if not email or email in seen_emails:
            continue
        seen_emails.add(email)
        result.append(c)
    return result


def _save_contact(
    db: Session, site: CustomerSite, contact: dict, source: str
) -> SiteContact | None:
    """Save a contact to a customer site, deduplicating by email."""
    email = (contact.get("email") or "").lower().strip()
    if not email:
        return None

    field_sources = contact.get("enrichment_field_sources") or {
        "email": source,
        "phone": source if contact.get("phone") else None,
        "name": source,
    }

    existing = (
        db.query(SiteContact)
        .filter_by(customer_site_id=site.id, email=email)
        .first()
    )
    if existing:
        # Update missing fields
        if contact.get("phone") and not existing.phone:
            existing.phone = contact["phone"]
            existing.phone_verified = _is_direct_dial(contact.get("phone_type"))
            # Update phone source attribution
            efs = existing.enrichment_field_sources or {}
            efs["phone"] = field_sources.get("phone", source)
            existing.enrichment_field_sources = efs
        if contact.get("linkedin_url") and not existing.linkedin_url:
            existing.linkedin_url = contact["linkedin_url"]
        if contact.get("title") and not existing.title:
            existing.title = contact["title"]
            existing.contact_role = _classify_contact_role(contact["title"])
        existing.last_enriched_at = datetime.now(timezone.utc)
        return existing

    sc = SiteContact(
        customer_site_id=site.id,
        full_name=contact.get("full_name") or "Unknown",
        title=contact.get("title"),
        email=email,
        phone=contact.get("phone"),
        phone_verified=_is_direct_dial(contact.get("phone_type")),
        enrichment_source=source,
        contact_role=_classify_contact_role(contact.get("title")),
        linkedin_url=contact.get("linkedin_url"),
        last_enriched_at=datetime.now(timezone.utc),
        enrichment_field_sources=field_sources,
    )
    db.add(sc)
    return sc


async def _step_lusha_phones(
    db: Session, contacts: list[dict], domain: str
) -> list[dict]:
    """Step 3: Enrich contacts with Lusha phone data (direct dials only).

    Skips contacts that already have a direct_dial or mobile phone.
    Calls find_person per contact, merges phone into contact dict.
    Records 1 credit per find_person call (even if no result).
    Stops early if credits are exhausted.
    """
    from ..connectors.lusha_client import find_person

    enriched = []
    for contact in contacts:
        # Skip if already has a direct dial or mobile
        if contact.get("phone") and contact.get("phone_type") in ("direct_dial", "mobile"):
            enriched.append(contact)
            continue

        if not can_use_credits(db, "lusha", 1):
            logger.info("Lusha credits exhausted during phone enrichment, stopping")
            enriched.append(contact)
            continue

        try:
            email = contact.get("email")
            full_name = contact.get("full_name") or ""
            parts = full_name.split(None, 1)
            first_name = parts[0] if parts else None
            last_name = parts[1] if len(parts) > 1 else None

            result = await find_person(
                email=email,
                first_name=first_name,
                last_name=last_name,
                company_domain=domain,
            )
            record_credit_usage(db, "lusha", 1)

            if result and result.get("phone"):
                contact["phone"] = result["phone"]
                contact["phone_type"] = result.get("phone_type", "direct_dial")
                fs = contact.get("enrichment_field_sources") or {}
                fs["phone"] = "lusha"
                contact["enrichment_field_sources"] = fs
                logger.debug("Lusha phone enriched %s → %s", email, result["phone"])
        except Exception as e:
            logger.debug("Lusha phone lookup failed for %s: %s", contact.get("email"), e)

        enriched.append(contact)

    return enriched



async def _step_hunter_verify(db: Session, contacts: list[dict]) -> list[dict]:
    """Step 3: Verify all contact emails via Hunter, reject non-deliverable."""
    from ..connectors.hunter_client import verify_email

    verified = []
    for contact in contacts:
        email = contact.get("email")
        if not email:
            continue

        if not can_use_credits(db, "hunter_verify", 1):
            logger.info("Hunter verify credits exhausted, keeping remaining unverified")
            contact["email_verified"] = False
            contact["email_verification_status"] = "unverified"
            verified.append(contact)
            continue

        result = await verify_email(email)
        record_credit_usage(db, "hunter_verify", 1)

        if result:
            status = result.get("status", "unknown")
            contact["email_verification_status"] = status
            if status in ("valid", "accept_all"):
                contact["email_verified"] = True
                verified.append(contact)
            else:
                logger.debug("Rejected email %s — status=%s", email, status)
        else:
            contact["email_verified"] = False
            contact["email_verification_status"] = "unknown"
            verified.append(contact)

    return verified


async def _step_apollo(
    db: Session, domain: str, company_name: str, needed: int
) -> list[dict]:
    """Step 1: Primary contact discovery via Apollo."""
    if not can_use_credits(db, "apollo", 1):
        logger.info("Apollo credits exhausted, skipping")
        return []

    try:
        from ..connectors.apollo_client import search_contacts as apollo_search

        raw = await apollo_search(
            company_name=company_name,
            domain=domain,
            limit=needed,
        )
        if raw:
            record_credit_usage(db, "apollo", 1)
            return [
                {
                    "full_name": c.get("full_name"),
                    "title": c.get("title"),
                    "email": c.get("email"),
                    "phone": c.get("phone"),
                    "phone_type": c.get("phone_type"),
                    "linkedin_url": c.get("linkedin_url"),
                    "source": "apollo",
                    "confidence": c.get("confidence", "medium"),
                    "enrichment_field_sources": {
                        "email": "apollo",
                        "name": "apollo",
                        "phone": "apollo" if c.get("phone") else None,
                    },
                }
                for c in raw
            ]
    except Exception as e:
        logger.debug("Apollo contact search failed: %s", e)
    return []


async def enrich_customer_account(
    company_id: int,
    db: Session,
    force: bool = False,
) -> dict:
    """Run the full waterfall enrichment for a customer account.

    Steps:
    1. Apollo — primary contact discovery (names, emails, titles)
    2. Hunter — verify all emails, reject non-deliverable
    3. Lusha — phone enrichment only (contacts missing direct dials)
    4. Apollo — last resort for remaining gaps — fill remaining gaps if still_needed > 0
    5. Dedup by email, validate quality, update company status

    Returns summary dict with contacts_added, contacts_verified, sources_used.
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

    site = _ensure_site(db, company)
    all_contacts = []
    sources_used = []

    # Step 1: Apollo (primary contact discovery)
    try:
        apollo_contacts = await _step_apollo(db, domain, company.name, needed)
    except Exception as e:
        logger.warning("Apollo step failed: %s", e)
        apollo_contacts = []

    if apollo_contacts:
        all_contacts.extend(apollo_contacts)
        sources_used.append("apollo")
        logger.info("Apollo returned %d contacts for %s", len(apollo_contacts), company.name)

    # Step 2: Hunter email verification
    if all_contacts:
        all_contacts = await _step_hunter_verify(db, all_contacts)
        sources_used.append("hunter_verify")

    # Step 3: Lusha phone enrichment (only contacts missing direct dials)
    if all_contacts:
        all_contacts = await _step_lusha_phones(db, all_contacts, domain)
        if any(
            (c.get("enrichment_field_sources") or {}).get("phone") == "lusha"
            for c in all_contacts
        ):
            sources_used.append("lusha_phones")

    # Final dedup
    all_contacts = _dedup_contacts(all_contacts)

    # Validate contacts before saving
    from .contact_quality import validate_contact
    validated = []
    for contact in all_contacts:
        is_valid, issues = validate_contact(contact)
        if is_valid:
            validated.append(contact)
        else:
            logger.debug("Skipping invalid contact: %s (%s)", contact.get("email"), issues)
    all_contacts = validated

    # Save contacts
    saved_count = 0
    for contact in all_contacts[:target]:
        source = contact.get("source", "unknown")
        sc = _save_contact(db, site, contact, source)
        if sc:
            if contact.get("email_verified"):
                sc.email_verified = True
                sc.email_verified_at = datetime.now(timezone.utc)
                sc.email_verification_status = contact.get("email_verification_status", "valid")
            saved_count += 1

    # Update company status
    company.customer_enrichment_at = datetime.now(timezone.utc)
    final_needed = _contacts_needed(db, company_id, target)
    if final_needed <= 0:
        company.customer_enrichment_status = "complete"
    elif saved_count > 0:
        company.customer_enrichment_status = "partial"
    else:
        company.customer_enrichment_status = "missing"

    db.flush()
    logger.info(
        "Customer enrichment for %s: %d contacts saved, sources=%s, status=%s",
        company.name, saved_count, sources_used, company.customer_enrichment_status,
    )

    return {
        "ok": True,
        "company_id": company_id,
        "contacts_added": saved_count,
        "contacts_verified": sum(1 for c in all_contacts if c.get("email_verified")),
        "sources_used": sources_used,
        "status": company.customer_enrichment_status,
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
            gaps.append({
                "company_id": co.id,
                "company_name": co.name,
                "domain": _get_company_domain(co),
                "account_owner_id": co.account_owner_id,
                "contacts_needed": needed,
                "current_status": co.customer_enrichment_status,
                "last_enriched": co.customer_enrichment_at.isoformat() if co.customer_enrichment_at else None,
            })
    return gaps
