"""Contact Quality Service — validation, dedup, scoring, and stale detection.

Provides data quality functions for customer contacts:
- validate_contact: check required fields and format
- dedup_contacts: merge duplicate contacts by email
- score_contact_completeness: 0-100 score based on filled fields
- flag_stale_contacts: mark contacts needing refresh
- compute_enrichment_status: derive company-level enrichment status

Called by: customer_enrichment_service.py, scheduler batch jobs.
Depends on: app.models.crm (SiteContact, Company, CustomerSite).
"""

import re
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..models.crm import Company, CustomerSite, SiteContact

# Minimum fields for a contact to be considered valid
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def validate_contact(contact: dict) -> tuple[bool, list[str]]:
    """Validate a contact dict. Returns (is_valid, list_of_issues)."""
    issues = []

    name = (contact.get("full_name") or "").strip()
    if not name or len(name) < 2:
        issues.append("missing_name")

    email = (contact.get("email") or "").strip()
    if not email:
        issues.append("missing_email")
    elif not _EMAIL_RE.match(email):
        issues.append("invalid_email_format")

    # Phone is optional but should be formatted if present
    phone = (contact.get("phone") or "").strip()
    if phone and len(phone) < 7:
        issues.append("phone_too_short")

    return len(issues) == 0, issues


def dedup_contacts(db: Session, site_id: int) -> int:
    """Merge duplicate contacts within a site by email. Returns count of merged."""
    contacts = (
        db.query(SiteContact)
        .filter_by(customer_site_id=site_id, is_active=True)
        .order_by(SiteContact.created_at)
        .all()
    )

    seen = {}
    merged = 0
    for c in contacts:
        email = (c.email or "").lower().strip()
        if not email:
            continue
        if email in seen:
            # Merge data into the first contact, deactivate the duplicate
            primary = seen[email]
            if c.phone and not primary.phone:
                primary.phone = c.phone
                primary.phone_verified = c.phone_verified
            if c.title and not primary.title:
                primary.title = c.title
            if c.linkedin_url and not primary.linkedin_url:
                primary.linkedin_url = c.linkedin_url
            if c.contact_role and not primary.contact_role:
                primary.contact_role = c.contact_role
            c.is_active = False
            merged += 1
        else:
            seen[email] = c

    if merged:
        db.flush()
    return merged


def score_contact_completeness(contact: SiteContact) -> int:
    """Score a contact 0-100 based on field completeness.

    Weights: email (25), name (20), phone (20), title (15), linkedin (10), verified (10).
    """
    score = 0
    if contact.email:
        score += 25
    if contact.full_name and contact.full_name != "Unknown":
        score += 20
    if contact.phone:
        score += 15
        if contact.phone_verified:
            score += 5
    if contact.title:
        score += 15
    if contact.linkedin_url:
        score += 10
    if contact.email_verified:
        score += 10
    return min(100, score)


def flag_stale_contacts(db: Session, stale_days: int = 180) -> int:
    """Flag contacts that haven't been enriched in stale_days. Returns count flagged."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)

    stale = (
        db.query(SiteContact)
        .filter(
            SiteContact.is_active == True,  # noqa: E712
            SiteContact.needs_refresh == False,  # noqa: E712
            (SiteContact.last_enriched_at.is_(None)) | (SiteContact.last_enriched_at < cutoff),
        )
        .all()
    )

    count = 0
    for c in stale:
        c.needs_refresh = True
        count += 1

    if count:
        db.flush()
        logger.info("Flagged %d stale contacts (older than %d days)", count, stale_days)
    return count


def compute_enrichment_status(db: Session, company_id: int) -> str:
    """Compute the enrichment status for a company based on its contacts.

    Returns: "complete", "partial", "missing", or "stale".
    """
    target = settings.customer_enrichment_contacts_per_account

    sites = db.query(CustomerSite.id).filter_by(company_id=company_id).all()
    if not sites:
        return "missing"

    site_ids = [s.id for s in sites]
    contacts = (
        db.query(SiteContact)
        .filter(
            SiteContact.customer_site_id.in_(site_ids),
            SiteContact.is_active == True,  # noqa: E712
        )
        .all()
    )

    if not contacts:
        return "missing"

    # Check if any need refresh
    stale_count = sum(1 for c in contacts if c.needs_refresh)
    if stale_count > len(contacts) // 2:
        return "stale"

    # Check completeness
    verified_count = sum(1 for c in contacts if c.email_verified)
    if len(contacts) >= target and verified_count >= target // 2:
        return "complete"

    if len(contacts) > 0:
        return "partial"

    return "missing"  # pragma: no cover — unreachable: line 155 already returns for empty


def update_company_enrichment_status(db: Session, company_id: int) -> str:
    """Compute and persist enrichment status for a company."""
    status = compute_enrichment_status(db, company_id)
    company = db.get(Company, company_id)
    if company:
        company.customer_enrichment_status = status
        db.flush()
    return status
