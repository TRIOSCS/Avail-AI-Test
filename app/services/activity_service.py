"""Activity service — zero-manual-logging engine.

Auto-creates activity_log records from system events (email, phone).
Matches contacts to companies or vendors, updates last_activity_at.

Usage:
    from app.services.activity_service import log_email_activity, log_call_activity, match_contact
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import ActivityLog, Company, CustomerSite, VendorCard, VendorContact

log = logging.getLogger("avail.activity")


# ═══════════════════════════════════════════════════════════════════════
#  CONTACT MATCHING — email or phone → company or vendor
# ═══════════════════════════════════════════════════════════════════════


def match_email_to_entity(email_addr: str, db: Session) -> dict | None:
    """Match an email address to a company or vendor card.

    Returns {"type": "company"|"vendor", "id": int, "name": str} or None.
    Checks customer sites first, then vendor contacts, then vendor card email lists.
    """
    if not email_addr:
        return None
    email_lower = email_addr.strip().lower()
    domain = email_lower.split("@")[-1] if "@" in email_lower else None

    # 1. Check customer_sites.contact_email (exact match)
    site = (
        db.query(CustomerSite)
        .filter(
            func.lower(CustomerSite.contact_email) == email_lower,
            CustomerSite.is_active.is_(True),
        )
        .first()
    )
    if site:
        return {"type": "company", "id": site.company_id, "name": site.site_name}

    # 2. Check vendor_contacts table (exact match)
    vc = (
        db.query(VendorContact)
        .filter(func.lower(VendorContact.email) == email_lower)
        .first()
    )
    if vc:
        card = db.get(VendorCard, vc.vendor_card_id)
        if card:
            return {"type": "vendor", "id": card.id, "name": card.display_name}

    # 3. Domain match against companies
    if domain and domain not in _GENERIC_DOMAINS:
        company = (
            db.query(Company)
            .filter(func.lower(Company.domain) == domain, Company.is_active.is_(True))
            .first()
        )
        if company:
            return {"type": "company", "id": company.id, "name": company.name}

    # 4. Domain match against vendor_cards
    if domain and domain not in _GENERIC_DOMAINS:
        vendor = (
            db.query(VendorCard)
            .filter(
                func.lower(VendorCard.domain) == domain,
                VendorCard.is_blacklisted.is_(False),
            )
            .first()
        )
        if vendor:
            return {"type": "vendor", "id": vendor.id, "name": vendor.display_name}

    return None


def match_phone_to_entity(phone: str, db: Session) -> dict | None:
    """Match a phone number to a company or vendor card.

    Simple normalized-suffix match (last 10 digits).
    """
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 7:
        return None
    suffix = digits[-10:]  # last 10 digits for matching

    # Check customer_sites
    sites = (
        db.query(CustomerSite)
        .filter(
            CustomerSite.contact_phone.isnot(None), CustomerSite.is_active.is_(True)
        )
        .all()
    )
    for site in sites:
        site_digits = "".join(c for c in (site.contact_phone or "") if c.isdigit())
        if site_digits and site_digits[-10:] == suffix:
            return {"type": "company", "id": site.company_id, "name": site.site_name}

    # Check vendor_contacts
    vcs = db.query(VendorContact).filter(VendorContact.phone.isnot(None)).all()
    for vc in vcs:
        vc_digits = "".join(c for c in (vc.phone or "") if c.isdigit())
        if vc_digits and vc_digits[-10:] == suffix:
            card = db.get(VendorCard, vc.vendor_card_id)
            if card:
                return {"type": "vendor", "id": card.id, "name": card.display_name}

    return None


# ═══════════════════════════════════════════════════════════════════════
#  ACTIVITY LOGGING
# ═══════════════════════════════════════════════════════════════════════


def log_email_activity(
    user_id: int,
    direction: str,  # "sent" or "received"
    email_addr: str,
    subject: str | None,
    external_id: str | None,
    contact_name: str | None,
    db: Session,
) -> ActivityLog | None:
    """Log an email activity, matching the contact to a company or vendor.

    Returns the ActivityLog record or None if dedup/no-match.
    """
    # Dedup by external_id
    if external_id:
        existing = (
            db.query(ActivityLog).filter(ActivityLog.external_id == external_id).first()
        )
        if existing:
            return None

    match = match_email_to_entity(email_addr, db)
    if not match:
        log.debug(f"No match for email {email_addr}, skipping activity log")
        return None

    activity_type = "email_sent" if direction == "sent" else "email_received"

    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel="email",
        company_id=match["id"] if match["type"] == "company" else None,
        vendor_card_id=match["id"] if match["type"] == "vendor" else None,
        contact_email=email_addr,
        contact_name=contact_name,
        subject=subject,
        external_id=external_id,
    )
    db.add(record)
    db.flush()

    # Update last_activity_at on the matched entity
    _update_last_activity(match, db, user_id)

    log.info(
        f"Activity logged: {activity_type} → {match['type']} '{match['name']}' by user {user_id}"
    )
    return record


def log_call_activity(
    user_id: int,
    direction: str,  # "outbound" or "inbound"
    phone: str,
    duration_seconds: int | None,
    external_id: str | None,
    contact_name: str | None,
    db: Session,
) -> ActivityLog | None:
    """Log a phone call activity."""
    if external_id:
        existing = (
            db.query(ActivityLog).filter(ActivityLog.external_id == external_id).first()
        )
        if existing:
            return None

    match = match_phone_to_entity(phone, db)
    if not match:
        log.debug(f"No match for phone {phone}, skipping activity log")
        return None

    activity_type = f"call_{direction}"

    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel="phone",
        company_id=match["id"] if match["type"] == "company" else None,
        vendor_card_id=match["id"] if match["type"] == "vendor" else None,
        contact_phone=phone,
        contact_name=contact_name,
        duration_seconds=duration_seconds,
        external_id=external_id,
    )
    db.add(record)
    db.flush()

    _update_last_activity(match, db, user_id)

    log.info(
        f"Activity logged: {activity_type} → {match['type']} '{match['name']}' by user {user_id}"
    )
    return record


# ═══════════════════════════════════════════════════════════════════════
#  QUERY HELPERS
# ═══════════════════════════════════════════════════════════════════════


def get_company_activities(
    company_id: int, db: Session, limit: int = 50
) -> list[ActivityLog]:
    """Get recent activity for a company."""
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.company_id == company_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .all()
    )


def get_vendor_activities(
    vendor_card_id: int, db: Session, limit: int = 50
) -> list[ActivityLog]:
    """Get recent activity for a vendor."""
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.vendor_card_id == vendor_card_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .all()
    )


def get_user_activities(
    user_id: int, db: Session, limit: int = 50
) -> list[ActivityLog]:
    """Get recent activity for a user."""
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.user_id == user_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .all()
    )


def days_since_last_activity(company_id: int, db: Session) -> int | None:
    """Days since last activity on a company. None if no activity ever."""
    latest = (
        db.query(func.max(ActivityLog.created_at))
        .filter(ActivityLog.company_id == company_id)
        .scalar()
    )
    if not latest:
        return None
    delta = datetime.now(timezone.utc) - latest.replace(tzinfo=timezone.utc)
    return delta.days


# ═══════════════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _update_last_activity(match: dict, db: Session, user_id: int | None = None):
    """Update last_activity_at on the matched company or vendor.

    For companies: also triggers open pool claim check if user_id provided.
    """
    now = datetime.now(timezone.utc)
    if match["type"] == "company":
        db.query(Company).filter(Company.id == match["id"]).update(
            {"last_activity_at": now}, synchronize_session=False
        )
        # Auto-claim open pool account if unowned
        if user_id:
            from app.services.ownership_service import check_and_claim_open_account

            check_and_claim_open_account(match["id"], user_id, db)
    elif match["type"] == "vendor":
        db.query(VendorCard).filter(VendorCard.id == match["id"]).update(
            {"last_activity_at": now}, synchronize_session=False
        )


# Skip generic email providers for domain matching
_GENERIC_DOMAINS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "aol.com",
        "icloud.com",
        "live.com",
        "msn.com",
        "protonmail.com",
        "mail.com",
        "yandex.com",
        "zoho.com",
        "gmx.com",
        "fastmail.com",
    }
)
