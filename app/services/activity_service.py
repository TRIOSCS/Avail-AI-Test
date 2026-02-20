"""Activity service — zero-manual-logging engine.

Auto-creates activity_log records from system events (email, phone).
Matches contacts to companies or vendors, updates last_activity_at.

Usage:
    from app.services.activity_service import log_email_activity, log_call_activity, match_contact
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

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
            return {"type": "vendor", "id": card.id, "name": card.display_name, "vendor_contact_id": vc.id}

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

    Uses SQL suffix match on PostgreSQL (regexp_replace), Python fallback on SQLite.
    Batch-loads VendorCards to avoid N+1.
    """
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 7:
        return None
    suffix = digits[-10:]  # last 10 digits for matching

    # Check customer_sites — try PostgreSQL regex, fall back to basic LIKE
    try:
        sites = (
            db.query(CustomerSite)
            .filter(
                CustomerSite.contact_phone.isnot(None),
                CustomerSite.is_active.is_(True),
                func.regexp_replace(CustomerSite.contact_phone, r"\D", "", "g").like(f"%{suffix}"),
            )
            .all()
        )
    except Exception:
        db.rollback()
        sites = (
            db.query(CustomerSite)
            .filter(CustomerSite.contact_phone.isnot(None), CustomerSite.is_active.is_(True))
            .all()
        )
    for site in sites:
        site_digits = "".join(c for c in (site.contact_phone or "") if c.isdigit())
        if site_digits and site_digits[-10:] == suffix:
            return {"type": "company", "id": site.company_id, "name": site.site_name}

    # Check vendor_contacts — batch VendorCard lookup to avoid N+1
    try:
        vcs = (
            db.query(VendorContact)
            .filter(
                VendorContact.phone.isnot(None),
                func.regexp_replace(VendorContact.phone, r"\D", "", "g").like(f"%{suffix}"),
            )
            .all()
        )
    except Exception:
        db.rollback()
        vcs = db.query(VendorContact).filter(VendorContact.phone.isnot(None)).all()
    if vcs:
        card_ids = [vc.vendor_card_id for vc in vcs if vc.vendor_card_id]
        card_map = (
            {c.id: c for c in db.query(VendorCard).filter(VendorCard.id.in_(card_ids)).all()}
            if card_ids else {}
        )
        for vc in vcs:
            vc_digits = "".join(c for c in (vc.phone or "") if c.isdigit())
            if vc_digits and vc_digits[-10:] == suffix:
                card = card_map.get(vc.vendor_card_id)
                if card:
                    return {"type": "vendor", "id": card.id, "name": card.display_name, "vendor_contact_id": vc.id}

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

    activity_type = "email_sent" if direction == "sent" else "email_received"

    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel="email",
        company_id=match["id"] if match and match["type"] == "company" else None,
        vendor_card_id=match["id"] if match and match["type"] == "vendor" else None,
        vendor_contact_id=match.get("vendor_contact_id") if match and match["type"] == "vendor" else None,
        contact_email=email_addr,
        contact_name=contact_name,
        subject=subject,
        external_id=external_id,
    )
    db.add(record)
    db.flush()

    if match:
        # Update last_activity_at on the matched entity
        _update_last_activity(match, db, user_id)
        _update_vendor_contact_stats(match, db)
        log.info(
            f"Activity logged: {activity_type} → {match['type']} '{match['name']}' by user {user_id}"
        )
    else:
        log.info(
            f"Activity logged (unmatched): {activity_type} for {email_addr} by user {user_id}"
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

    activity_type = f"call_{direction}"

    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel="phone",
        company_id=match["id"] if match and match["type"] == "company" else None,
        vendor_card_id=match["id"] if match and match["type"] == "vendor" else None,
        vendor_contact_id=match.get("vendor_contact_id") if match and match["type"] == "vendor" else None,
        contact_phone=phone,
        contact_name=contact_name,
        duration_seconds=duration_seconds,
        external_id=external_id,
    )
    db.add(record)
    db.flush()

    if match:
        _update_last_activity(match, db, user_id)
        _update_vendor_contact_stats(match, db)
        log.info(
            f"Activity logged: {activity_type} → {match['type']} '{match['name']}' by user {user_id}"
        )
    else:
        log.info(
            f"Activity logged (unmatched): {activity_type} for {phone} by user {user_id}"
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


# ═══════════════════════════════════════════════════════════════════════
#  COMPANY-SPECIFIC MANUAL LOGGING
# ═══════════════════════════════════════════════════════════════════════


def log_company_call(
    user_id: int,
    company_id: int,
    direction: str,
    phone: str | None,
    duration_seconds: int | None,
    contact_name: str | None,
    notes: str | None,
    db: Session,
) -> ActivityLog:
    """Log a manual call against a company."""
    activity_type = f"call_{direction}"
    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel="phone",
        company_id=company_id,
        contact_phone=phone,
        contact_name=contact_name,
        duration_seconds=duration_seconds,
        notes=notes,
    )
    db.add(record)
    db.flush()

    now = datetime.now(timezone.utc)
    db.query(Company).filter(Company.id == company_id).update(
        {"last_activity_at": now}, synchronize_session=False
    )
    log.info(f"Activity logged: {activity_type} -> company {company_id} by user {user_id}")
    return record


def log_company_note(
    user_id: int,
    company_id: int,
    contact_name: str | None,
    notes: str,
    db: Session,
) -> ActivityLog:
    """Log a manual note against a company."""
    record = ActivityLog(
        user_id=user_id,
        activity_type="note",
        channel="manual",
        company_id=company_id,
        contact_name=contact_name,
        notes=notes,
    )
    db.add(record)
    db.flush()

    now = datetime.now(timezone.utc)
    db.query(Company).filter(Company.id == company_id).update(
        {"last_activity_at": now}, synchronize_session=False
    )
    log.info(f"Activity logged: note -> company {company_id} by user {user_id}")
    return record


# ═══════════════════════════════════════════════════════════════════════
#  VENDOR-SPECIFIC MANUAL LOGGING
# ═══════════════════════════════════════════════════════════════════════


def log_vendor_call(
    user_id: int,
    vendor_card_id: int,
    vendor_contact_id: int | None,
    direction: str,
    phone: str | None,
    duration_seconds: int | None,
    contact_name: str | None,
    notes: str | None,
    db: Session,
    requisition_id: int | None = None,
) -> ActivityLog:
    """Log a manual call against a known vendor (from vendor popup)."""
    activity_type = f"call_{direction}"
    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel="phone",
        vendor_card_id=vendor_card_id,
        vendor_contact_id=vendor_contact_id,
        contact_phone=phone,
        contact_name=contact_name,
        duration_seconds=duration_seconds,
        notes=notes,
        requisition_id=requisition_id,
    )
    db.add(record)
    db.flush()

    now = datetime.now(timezone.utc)
    db.query(VendorCard).filter(VendorCard.id == vendor_card_id).update(
        {"last_activity_at": now}, synchronize_session=False
    )
    if vendor_contact_id:
        _increment_vendor_contact(vendor_contact_id, db)

    log.info(f"Activity logged: {activity_type} -> vendor {vendor_card_id} by user {user_id}")
    return record


def log_vendor_note(
    user_id: int,
    vendor_card_id: int,
    vendor_contact_id: int | None,
    notes: str,
    contact_name: str | None,
    db: Session,
    requisition_id: int | None = None,
) -> ActivityLog:
    """Log a manual note against a vendor."""
    record = ActivityLog(
        user_id=user_id,
        activity_type="note",
        channel="manual",
        vendor_card_id=vendor_card_id,
        vendor_contact_id=vendor_contact_id,
        contact_name=contact_name,
        notes=notes,
        requisition_id=requisition_id,
    )
    db.add(record)
    db.flush()

    now = datetime.now(timezone.utc)
    db.query(VendorCard).filter(VendorCard.id == vendor_card_id).update(
        {"last_activity_at": now}, synchronize_session=False
    )
    if vendor_contact_id:
        _increment_vendor_contact(vendor_contact_id, db)

    log.info(f"Activity logged: note -> vendor {vendor_card_id} by user {user_id}")
    return record


def days_since_last_vendor_activity(vendor_card_id: int, db: Session) -> int | None:
    """Days since last activity on a vendor card. None if no activity ever."""
    latest = (
        db.query(func.max(ActivityLog.created_at))
        .filter(ActivityLog.vendor_card_id == vendor_card_id)
        .scalar()
    )
    if not latest:
        return None
    delta = datetime.now(timezone.utc) - latest.replace(tzinfo=timezone.utc)
    return delta.days


def _update_vendor_contact_stats(match: dict, db: Session):
    """Increment interaction_count and set last_interaction_at on matched VendorContact."""
    vc_id = match.get("vendor_contact_id")
    if vc_id:
        _increment_vendor_contact(vc_id, db)


def _increment_vendor_contact(vendor_contact_id: int, db: Session):
    """Increment a VendorContact's interaction stats."""
    now = datetime.now(timezone.utc)
    db.query(VendorContact).filter(VendorContact.id == vendor_contact_id).update(
        {
            "interaction_count": VendorContact.interaction_count + 1,
            "last_interaction_at": now,
        },
        synchronize_session=False,
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


# ═══════════════════════════════════════════════════════════════════════
#  UNMATCHED ACTIVITY QUEUE (Phase 2A)
# ═══════════════════════════════════════════════════════════════════════


def get_unmatched_activities(
    db: Session, limit: int = 100, offset: int = 0
) -> list[ActivityLog]:
    """Get activities with no company or vendor match and not dismissed."""
    return (
        db.query(ActivityLog)
        .filter(
            ActivityLog.company_id.is_(None),
            ActivityLog.vendor_card_id.is_(None),
            ActivityLog.dismissed_at.is_(None),
        )
        .order_by(ActivityLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def count_unmatched_activities(db: Session) -> int:
    """Count unmatched, non-dismissed activities."""
    return (
        db.query(func.count(ActivityLog.id))
        .filter(
            ActivityLog.company_id.is_(None),
            ActivityLog.vendor_card_id.is_(None),
            ActivityLog.dismissed_at.is_(None),
        )
        .scalar()
        or 0
    )


def attribute_activity(
    activity_id: int,
    entity_type: str,
    entity_id: int,
    db: Session,
    user_id: int | None = None,
) -> ActivityLog | None:
    """Attribute an unmatched activity to a company or vendor.

    Returns the updated ActivityLog or None if not found.
    """
    activity = db.get(ActivityLog, activity_id)
    if not activity:
        return None

    if entity_type == "company":
        activity.company_id = entity_id
        activity.vendor_card_id = None
    elif entity_type == "vendor":
        activity.vendor_card_id = entity_id
        activity.company_id = None
    else:
        return None

    db.flush()

    # Also update last_activity_at on the target entity
    match = {"type": entity_type, "id": entity_id}
    _update_last_activity(match, db, user_id)

    log.info(
        f"Activity {activity_id} attributed to {entity_type} {entity_id}"
    )
    return activity


def dismiss_activity(activity_id: int, db: Session) -> ActivityLog | None:
    """Dismiss an unmatched activity (mark as reviewed, not useful)."""
    activity = db.get(ActivityLog, activity_id)
    if not activity:
        return None

    activity.dismissed_at = datetime.now(timezone.utc)
    db.flush()

    log.info(f"Activity {activity_id} dismissed")
    return activity
