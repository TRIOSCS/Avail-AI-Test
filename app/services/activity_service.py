"""Activity service — zero-manual-logging engine.

Auto-creates activity_log records from system events (email, phone).
Matches contacts to companies or vendors, updates last_activity_at.

Usage:
    from app.services.activity_service import log_email_activity, log_call_activity, match_contact
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.constants import (
    MEANINGFUL_CALL_OUTCOMES,
    ActivityType,
    Channel,
    Direction,
    EventType,
    InboxSyncHealth,
    OutreachChannel,
)
from app.models import ActivityLog, Company, CustomerSite, SiteContact, VendorCard, VendorContact
from app.utils.phone import normalize_e164
from app.utils.token_manager import _utc
from app.vendor_utils import GENERIC_EMAIL_DOMAINS as _GENERIC_DOMAINS

# Minimum connected-call duration to be considered a real conversation.
# Calls shorter than this (including duration=0 or None) are voicemails or
# missed calls — they do NOT advance the reply clock.
CALL_MEANINGFUL_MIN_SECONDS: int = 30

# Activity types that are inherently meaningful — flagged is_meaningful=True at
# write time (cheap, deterministic). The high-volume / free-text types
# (sighting_added, email_received) are deliberately excluded: they are left
# is_meaningful=None for the AI quality-scoring pass to classify. Call events
# are flagged in log_call_activity (they are not written via log_activity).
_RULE_MEANINGFUL_TYPES: frozenset[str] = frozenset(
    {
        ActivityType.RFQ_SENT,
        ActivityType.STATUS_CHANGED,
        ActivityType.OFFER_CREATED,
        ActivityType.OFFER_STATUS_CHANGED,
        ActivityType.ASSIGNMENT_CHANGED,
        ActivityType.TASK_COMPLETED,
    }
)

# ═══════════════════════════════════════════════════════════════════════
#  CONTACT MATCHING — email or phone → company or vendor
# ═══════════════════════════════════════════════════════════════════════


def _resolve_site_contact(email_lower: str, company_id: int, site_id: int | None, db: Session) -> int | None:
    """Return the SiteContact.id whose email matches email_lower within the company's
    sites.

    Scoped to the matched site when known, otherwise searches all active sites for the
    company. Prefers email_verified=True, then is_primary=True, then first found.
    Returns None if no match — NO change to existing behaviour.
    """
    q = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(
            func.lower(SiteContact.email) == email_lower,
            CustomerSite.company_id == company_id,
            CustomerSite.is_active.is_(True),
        )
    )
    if site_id is not None:
        q = q.filter(SiteContact.customer_site_id == site_id)
    candidates = q.all()
    if not candidates:
        return None
    # Prefer verified, then primary, then first
    for sc in candidates:
        if sc.email_verified:
            return sc.id
    for sc in candidates:
        if sc.is_primary:
            return sc.id
    return candidates[0].id


def match_email_to_entity(email_addr: str, db: Session) -> dict | None:
    """Match an email address to a company or vendor card.

    Returns {"type": "company"|"vendor", "id": int, "name": str} or None. Checks
    customer sites first, then vendor contacts, then vendor card email lists.

    Resolution order (most-specific wins):
      1. Exact SiteContact.email match (highest-confidence — personal address)
      2. Exact CustomerSite.contact_email match
      3. Exact VendorContact.email match
      4. Domain → Company match; on multi-match fuzzy-scores against the email
         local-part via fuzzy_score_vendor and picks the best-scoring entity.
      5. Domain → VendorCard match; same fuzzy tie-break on multi-match.

    For customer-side matches also resolves the SiteContact whose email equals the
    address and includes site_contact_id in the result (None when no contact found).
    """
    if not email_addr:
        return None
    email_lower = email_addr.strip().lower()
    domain = email_lower.split("@")[-1] if "@" in email_lower else None
    local_part = email_lower.split("@")[0] if "@" in email_lower else email_lower

    # 1. Exact SiteContact.email match — resolves directly to company without
    #    needing an intermediate CustomerSite lookup.
    # Order: verified first, then primary, then lowest id — mirrors _resolve_site_contact.
    sc_exact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(
            func.lower(SiteContact.email) == email_lower,
            CustomerSite.is_active.is_(True),
        )
        .order_by(
            SiteContact.email_verified.desc(),
            SiteContact.is_primary.desc(),
            SiteContact.id.asc(),
        )
        .first()
    )
    if sc_exact:
        site = db.get(CustomerSite, sc_exact.customer_site_id)
        if site:
            return {
                "type": "company",
                "id": site.company_id,
                "name": site.site_name,
                "site_id": site.id,
                "site_contact_id": sc_exact.id,
            }

    # 2. Exact CustomerSite.contact_email match
    site = (
        db.query(CustomerSite)
        .filter(
            func.lower(CustomerSite.contact_email) == email_lower,
            CustomerSite.is_active.is_(True),
        )
        .first()
    )
    if site:
        sc_id = _resolve_site_contact(email_lower, site.company_id, site.id, db)
        return {
            "type": "company",
            "id": site.company_id,
            "name": site.site_name,
            "site_id": site.id,
            "site_contact_id": sc_id,
        }

    # 3. Exact VendorContact.email match
    vc = db.query(VendorContact).filter(func.lower(VendorContact.email) == email_lower).first()
    if vc:
        card = db.get(VendorCard, vc.vendor_card_id)
        if card:
            return {"type": "vendor", "id": card.id, "name": card.display_name, "vendor_contact_id": vc.id}

    if not domain or domain in _GENERIC_DOMAINS:
        return None

    # 4. Domain match against companies — fuzzy tie-break when multiple match.
    companies = db.query(Company).filter(func.lower(Company.domain) == domain, Company.is_active.is_(True)).all()
    if companies:
        from app.vendor_utils import fuzzy_score_vendor

        best = max(companies, key=lambda c: fuzzy_score_vendor(local_part, c.name))
        sc_id = _resolve_site_contact(email_lower, best.id, None, db)
        return {"type": "company", "id": best.id, "name": best.name, "site_contact_id": sc_id}

    # 5. Domain match against vendor_cards — fuzzy tie-break when multiple match.
    vendors = (
        db.query(VendorCard)
        .filter(
            func.lower(VendorCard.domain) == domain,
            VendorCard.is_blacklisted.is_(False),
        )
        .all()
    )
    if vendors:
        if len(vendors) == 1:
            return {"type": "vendor", "id": vendors[0].id, "name": vendors[0].display_name}
        from app.vendor_utils import fuzzy_score_vendor

        best = max(vendors, key=lambda v: fuzzy_score_vendor(local_part, v.display_name))
        return {"type": "vendor", "id": best.id, "name": best.display_name}

    return None


def _phone_digits(phone: str | None) -> str:
    """Return only the digit characters of a phone string ("" if none)."""
    return "".join(c for c in (phone or "") if c.isdigit())


def _match_vendor_card_by_phone(db: Session, e164: str) -> VendorCard | None:
    """Find one non-blacklisted VendorCard whose normalized_phones contains e164.

    Pushes the membership test into the database instead of materializing every
    non-blacklisted vendor card into Python.

    Postgres: uses JSONB containment (``@>``). ``normalized_phones`` is a plain
    ``JSON`` column (no ``@>``), so it is cast to ``JSONB`` at query time. SQLite
    (the test DB) has neither ``JSONB`` nor ``@>``, so it falls back to a Python
    membership test over the (blacklist-filtered) rows — the observable result is
    identical; only Postgres gets the indexed/in-DB path.
    """
    base = db.query(VendorCard).filter(VendorCard.is_blacklisted.is_(False))
    if bool(db.bind) and db.bind.dialect.name == "postgresql":
        return base.filter(VendorCard.normalized_phones.cast(JSONB).contains([e164])).first()
    return next((c for c in base.all() if e164 in (c.normalized_phones or [])), None)


def match_phone_to_entity(phone: str, db: Session) -> dict | None:
    """Unified E.164 phone matcher — queries normalized columns in priority order.

    Collects ALL distinct entity matches (company_id XOR vendor_card_id) across
    five priority tiers and returns the highest-priority match dict. Sets
    ambiguous=True when more than one distinct entity matched.

    Priority order:
      1. SiteContact.normalized_phone
      2. Company.normalized_phone
      3. CustomerSite.normalized_phone / normalized_phone_2
      4. VendorContact.normalized_phone
      5. VendorCard.normalized_phones (JSON containment, fallback only —
         skipped entirely when priorities 1-4 already matched)

    Returns None if the number is unparseable or no match found.
    """
    e164 = normalize_e164(phone)
    if e164 is None:
        return None

    seen: set[tuple] = set()
    candidates: list[dict] = []

    # ── Priority 1: SiteContact ──────────────────────────────────────────
    contacts = db.query(SiteContact).filter(SiteContact.normalized_phone == e164, SiteContact.is_active.is_(True)).all()
    for contact in contacts:
        site = db.get(CustomerSite, contact.customer_site_id)
        company_id = site.company_id if site else None
        if company_id is None:
            continue
        company = db.get(Company, company_id)
        key = ("company", company_id)
        if key not in seen:
            seen.add(key)
            candidates.append(
                {
                    "type": "company",
                    "id": company_id,
                    "name": company.name if company else None,
                    "source": "site_contact",
                    "company_id": company_id,
                    "vendor_card_id": None,
                    "_site_id": site.id if site else None,
                    "_site_contact_id": contact.id,
                    "_contact_name": contact.full_name,
                    "_vendor_contact_id": None,
                }
            )

    # ── Priority 2: Company ──────────────────────────────────────────────
    companies = db.query(Company).filter(Company.normalized_phone == e164, Company.is_active.is_(True)).all()
    for company in companies:
        key = ("company", company.id)
        if key not in seen:
            seen.add(key)
            candidates.append(
                {
                    "type": "company",
                    "id": company.id,
                    "name": company.name,
                    "source": "company",
                    "company_id": company.id,
                    "vendor_card_id": None,
                    "_site_id": None,
                    "_site_contact_id": None,
                    "_contact_name": None,
                    "_vendor_contact_id": None,
                }
            )

    # ── Priority 3: CustomerSite ─────────────────────────────────────────
    sites = (
        db.query(CustomerSite)
        .filter(
            (CustomerSite.normalized_phone == e164) | (CustomerSite.normalized_phone_2 == e164),
            CustomerSite.is_active.is_(True),
        )
        .all()
    )
    for site in sites:
        key = ("company", site.company_id)
        if key not in seen:
            seen.add(key)
            company = db.get(Company, site.company_id)
            candidates.append(
                {
                    "type": "company",
                    "id": site.company_id,
                    "name": company.name if company else site.site_name,
                    "source": "customer_site",
                    "company_id": site.company_id,
                    "vendor_card_id": None,
                    "_site_id": site.id,
                    "_site_contact_id": None,
                    "_contact_name": None,
                    "_vendor_contact_id": None,
                }
            )

    # ── Priority 4: VendorContact ────────────────────────────────────────
    vcs = db.query(VendorContact).filter(VendorContact.normalized_phone == e164).all()
    for vc in vcs:
        card = db.get(VendorCard, vc.vendor_card_id) if vc.vendor_card_id else None
        if card is None:
            continue
        key = ("vendor", card.id)
        if key not in seen:
            seen.add(key)
            candidates.append(
                {
                    "type": "vendor",
                    "id": card.id,
                    "name": card.display_name,
                    "source": "vendor_contact",
                    "company_id": None,
                    "vendor_card_id": card.id,
                    "_site_id": None,
                    "_site_contact_id": None,
                    "_contact_name": None,
                    "_vendor_contact_id": vc.id,
                }
            )

    # ── Priority 5: VendorCard normalized_phones (fallback) ──────────────
    # Only runs when priorities 1-4 produced no match. This is the last-resort
    # JSON-containment lookup — it must never execute on the hot inbound/outbound
    # call-logging path when a higher-priority normalized-column match already
    # exists, and it must NOT materialize every non-blacklisted vendor card.
    if not candidates:
        card = _match_vendor_card_by_phone(db, e164)
        if card is not None:
            key = ("vendor", card.id)
            if key not in seen:
                seen.add(key)
                candidates.append(
                    {
                        "type": "vendor",
                        "id": card.id,
                        "name": card.display_name,
                        "source": "vendor_card",
                        "company_id": None,
                        "vendor_card_id": card.id,
                        "_site_id": None,
                        "_site_contact_id": None,
                        "_contact_name": None,
                        "_vendor_contact_id": None,
                    }
                )

    if not candidates:
        return None

    best = candidates[0]
    ambiguous = len(candidates) > 1

    # Build public candidate list (no internal _ fields)
    public_candidates = [
        {
            "type": c["type"],
            "id": c["id"],
            "name": c["name"],
            "source": c["source"],
            "company_id": c["company_id"],
            "vendor_card_id": c["vendor_card_id"],
        }
        for c in candidates
    ]

    if best["type"] == "company":
        return {
            "type": "company",
            "id": best["company_id"],
            "name": best["name"],
            "company_id": best["company_id"],
            "site_id": best["_site_id"],  # backward compat for _match_entity_links
            "customer_site_id": best["_site_id"],
            "site_contact_id": best["_site_contact_id"],
            "contact_name": best.get("_contact_name"),
            "vendor_card_id": None,
            "vendor_contact_id": None,
            "ambiguous": ambiguous,
            "candidates": public_candidates,
        }
    # vendor
    return {
        "type": "vendor",
        "id": best["vendor_card_id"],
        "name": best["name"],
        "company_id": None,
        "site_id": None,
        "customer_site_id": None,
        "site_contact_id": None,
        "contact_name": None,
        "vendor_card_id": best["vendor_card_id"],
        "vendor_contact_id": best["_vendor_contact_id"],
        "ambiguous": ambiguous,
        "candidates": public_candidates,
    }


# ═══════════════════════════════════════════════════════════════════════
#  ACTIVITY LOGGING
# ═══════════════════════════════════════════════════════════════════════


def _match_entity_links(match: dict | None) -> dict:
    """Translate a contact-match dict into ActivityLog entity-link kwargs.

    Returns company_id / vendor_card_id / vendor_contact_id / customer_site_id /
    site_contact_id with only the matched side populated (the other side stays None).
    """
    if match and match["type"] == "company":
        return {
            "company_id": match["id"],
            "vendor_card_id": None,
            "vendor_contact_id": None,
            "customer_site_id": match.get("site_id"),
            "site_contact_id": match.get("site_contact_id"),
        }
    if match and match["type"] == "vendor":
        return {
            "company_id": None,
            "vendor_card_id": match["id"],
            "vendor_contact_id": match.get("vendor_contact_id"),
            "customer_site_id": None,
            "site_contact_id": None,
        }
    return {
        "company_id": None,
        "vendor_card_id": None,
        "vendor_contact_id": None,
        "customer_site_id": None,
        "site_contact_id": None,
    }


def log_email_activity(
    user_id: int | None,
    direction: str,  # sent/received/inbound/outbound (normalized via _normalize_direction)
    email_addr: str,
    subject: str | None,
    external_id: str | None,
    contact_name: str | None,
    db: Session,
    requisition_id: int | None = None,
    requirement_id: int | None = None,
    occurred_at: datetime | None = None,
) -> ActivityLog | None:
    """Log an email activity, matching the contact to a company or vendor.

    Returns the ActivityLog record or None if dedup/no-match.

    Pass occurred_at to stamp the exact send/receive time on the row.  When omitted the
    column default (server-side UTC now) is used.  Always pass occurred_at for send-time
    rows so the scan_sent_folder reconcile query (which filters ActivityLog.occurred_at
    >= reconcile_window_start) can match the row and avoid creating a duplicate.
    """
    # Dedup by external_id
    if external_id:
        existing = db.query(ActivityLog).filter(ActivityLog.external_id == external_id).first()
        if existing:
            return None

    # Normalize direction the same way log_call_activity does so 'outbound'/'inbound'/
    # 'sent'/'received' all classify correctly. Email rows must carry a definite direction
    # (EMAIL_SENT vs EMAIL_RECEIVED), so an unrecognized value is rejected explicitly
    # rather than silently mis-bucketed as INBOUND / EMAIL_RECEIVED.
    normalized_direction = _normalize_direction(direction)
    if normalized_direction is None:
        raise ValueError(
            f"log_email_activity: unrecognized direction {direction!r}; expected one of sent/received/inbound/outbound"
        )
    is_outbound = normalized_direction == Direction.OUTBOUND

    match = match_email_to_entity(email_addr, db)

    activity_type = ActivityType.EMAIL_SENT if is_outbound else ActivityType.EMAIL_RECEIVED

    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel=Channel.EMAIL,
        **_match_entity_links(match),
        contact_email=email_addr,
        contact_name=contact_name,
        subject=subject,
        external_id=external_id,
        direction=normalized_direction,
        event_type=EventType.EMAIL,
        summary=f"Email {'to' if is_outbound else 'from'} {contact_name or email_addr}",
        requisition_id=requisition_id,
        requirement_id=requirement_id,
        occurred_at=occurred_at,
    )
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)

    if match:
        # Update last_activity_at on the matched entity
        _update_last_activity(match, db, user_id)
        _update_vendor_contact_stats(match, db)
        logger.info(f"Activity logged: {activity_type} → {match['type']} '{match['name']}' by user {user_id}")
    else:
        logger.info(f"Activity logged (unmatched): {activity_type} for {email_addr} by user {user_id}")

    return record


def log_meeting_activity(
    user_id: int | None,
    graph_event_id: str,
    subject: str | None,
    start_dt: datetime,
    end_dt: datetime,
    organizer_email: str | None,
    attendee_emails: list[str],
    location: str | None,
    db: Session,
) -> list["ActivityLog"]:
    """Log a calendar meeting, linking each matched external attendee.

    Mirrors log_email_activity: resolves attendees via match_email_to_entity +
    _match_entity_links, filters own-domain / generic / junk addresses, and calls
    bump_clocks_from_activity for each linked row.

    Returns the list of ActivityLog rows created (one per matched external entity).
    Returns [] if only internal attendees or the event was already logged.
    Dedup key: ``external_id = "calendar-{graph_event_id}"``.
    """
    from app.config import settings
    from app.shared_constants import JUNK_DOMAINS, JUNK_EMAIL_PREFIXES

    external_id = f"calendar-{graph_event_id}"

    # Idempotent re-scan: skip if ANY row with this external_id already exists.
    existing = db.query(ActivityLog).filter(ActivityLog.external_id == external_id).first()
    if existing:
        return []

    own_domains: frozenset[str] = settings.own_domains
    _all_generic = _GENERIC_DOMAINS | JUNK_DOMAINS

    def _is_internal(email: str) -> bool:
        domain = email.split("@")[-1] if "@" in email else ""
        return domain in own_domains

    def _is_junk(email: str) -> bool:
        domain = email.split("@")[-1] if "@" in email else ""
        local = email.split("@")[0] if "@" in email else email
        if domain in _all_generic:
            return True
        if local in JUNK_EMAIL_PREFIXES:
            return True
        return False

    organizer_lower = (organizer_email or "").strip().lower()
    organizer_is_own = _is_internal(organizer_lower) if organizer_lower else True
    direction = Direction.OUTBOUND if organizer_is_own else Direction.INBOUND
    duration_seconds = max(0, int((end_dt - start_dt).total_seconds()))

    from .cadence_service import bump_clocks_from_activity

    rows: list[ActivityLog] = []
    matched_entity_keys: set[str] = set()

    for raw_email in attendee_emails:
        email_lower = raw_email.strip().lower()
        if not email_lower or "@" not in email_lower:
            continue
        if _is_internal(email_lower):
            continue
        if _is_junk(email_lower):
            continue

        match = match_email_to_entity(email_lower, db)
        if not match:
            continue

        # Deduplicate: one row per entity per event (3-person meeting = 3 rows but same
        # entity must not appear twice if attendee list has duplicates).
        entity_key = f"{match['type']}:{match['id']}"
        if entity_key in matched_entity_keys:
            continue
        matched_entity_keys.add(entity_key)

        record = ActivityLog(
            user_id=user_id,
            activity_type=ActivityType.MEETING,
            channel=Channel.CALENDAR,
            **_match_entity_links(match),
            subject=(subject or "")[:500] or None,
            external_id=external_id,
            direction=direction,
            event_type=EventType.MEETING,
            is_meaningful=True,
            duration_seconds=duration_seconds,
            occurred_at=start_dt,
            details={
                "attendees": attendee_emails[:20],
                "organizer": organizer_email,
                "location": location,
                "subject": subject,
                "graph_event_id": graph_event_id,
            },
            summary=f"Meeting: {subject or '(no subject)'}",
        )
        db.add(record)
        db.flush()

        bump_clocks_from_activity(db, record)
        _update_last_activity(match, db, user_id)
        _update_vendor_contact_stats(match, db)
        logger.info(
            "Meeting activity logged: {} → {} '{}' by user {}",
            graph_event_id,
            match["type"],
            match["name"],
            user_id,
        )
        rows.append(record)

    return rows


def _normalize_direction(direction: str | None) -> str | None:
    """Canonicalize a direction input to a stored Direction value or None.

    sent->outbound, received->inbound, inbound/outbound pass through; anything else
    (None, 'unknown', ...) is stored as NULL — never a sentinel string.
    """
    return {
        "sent": Direction.OUTBOUND,
        "received": Direction.INBOUND,
        "inbound": Direction.INBOUND,
        "outbound": Direction.OUTBOUND,
    }.get((direction or "").strip().lower())


# Sentinel: distinguishes "caller did not pass a match" from "caller passed None (no match found)".
_NO_MATCH = object()


def log_call_activity(
    user_id: int | None,
    direction: str | None,  # accepts sent/received/inbound/outbound/None
    phone: str,
    duration_seconds: int | None,
    external_id: str | None,
    contact_name: str | None,
    db: Session,
    subject: str | None = None,
    requisition_id: int | None = None,
    requirement_id: int | None = None,
    force_meaningful: bool | None = None,
    occurred_at: datetime | None = None,
    details: dict | None = None,
    match_result: object = _NO_MATCH,
) -> ActivityLog | None:
    """Log a phone call activity.

    Pass occurred_at to stamp the true call time on the row (e.g. from 8x8 CDR). Pass
    details to store structured metadata (e.g. call_outcome, department, source). When
    details carries a call_outcome, is_meaningful is determined by whether the outcome
    is CONNECTED; otherwise the existing duration >= 30s gate applies.
    """
    direction = _normalize_direction(direction)
    if external_id:
        existing = db.query(ActivityLog).filter(ActivityLog.external_id == external_id).first()
        if existing:
            return None

    match = match_phone_to_entity(phone, db) if match_result is _NO_MATCH else match_result

    activity_type = ActivityType.CALL_LOGGED

    # Auto-generate subject if not explicitly provided
    if not subject:
        verb = "to" if direction == "outbound" else "from"
        target = contact_name or phone or "unknown"
        subject = f"Call {verb} {target}"

    # is_meaningful logic: outcome-gate takes priority over duration-gate
    if force_meaningful is not None:
        is_meaningful = force_meaningful
    elif details and details.get("call_outcome"):
        is_meaningful = details["call_outcome"] in MEANINGFUL_CALL_OUTCOMES
    else:
        is_meaningful = duration_seconds is not None and duration_seconds >= CALL_MEANINGFUL_MIN_SECONDS

    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel=Channel.PHONE,
        **_match_entity_links(match),
        contact_phone=phone,
        contact_name=contact_name,
        duration_seconds=duration_seconds,
        external_id=external_id,
        subject=subject,
        direction=direction,
        event_type=EventType.CALL,
        summary=subject,
        requisition_id=requisition_id,
        requirement_id=requirement_id,
        occurred_at=occurred_at,
        details=details,
        is_meaningful=is_meaningful,
    )
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)

    if match:
        _update_last_activity(match, db, user_id)
        _update_vendor_contact_stats(match, db)
        logger.info(f"Activity logged: {activity_type} → {match['type']} '{match['name']}' by user {user_id}")
    else:
        logger.info(f"Activity logged (unmatched): {activity_type} for {phone} by user {user_id}")

    return record


# ═══════════════════════════════════════════════════════════════════════
#  QUERY HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _is_meaningful_or_unscored():
    """Filter clause keeping rows the AI quality pass marked meaningful (True) or has
    not yet scored (None) — i.e. hide only is_meaningful=False."""
    return ActivityLog.is_meaningful.is_(True) | ActivityLog.is_meaningful.is_(None)


def get_company_activities(
    company_id: int, db: Session, limit: int = 50, meaningful_only: bool = False
) -> list[ActivityLog]:
    """Get recent activity for a company.

    meaningful_only (default False preserves existing caller behaviour). When True,
    filters out activities that the AI quality pass classified as not meaningful
    (is_meaningful=False); rows that are meaningful (True) or not yet scored (None) are
    kept, matching the requisition path semantics.
    """
    q = db.query(ActivityLog).filter(ActivityLog.company_id == company_id)
    if meaningful_only:
        q = q.filter(_is_meaningful_or_unscored())
    return q.order_by(ActivityLog.created_at.desc()).limit(limit).all()


def get_vendor_activities(vendor_card_id: int, db: Session, limit: int = 50) -> list[ActivityLog]:
    """Get recent activity for a vendor."""
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.vendor_card_id == vendor_card_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .all()
    )


def get_user_activities(user_id: int, db: Session, limit: int = 50) -> list[ActivityLog]:
    """Get recent activity for a user."""
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.user_id == user_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .all()
    )


def get_requisition_activities(
    requisition_id: int, db: Session, limit: int = 200, meaningful_only: bool = True
) -> list[ActivityLog]:
    """Get the activity timeline for a requisition, newest first.

    Backs the requisition Activity tab. Uses the ix_activity_requisition index.

    meaningful_only (default True) hides events the AI quality pass classified as not
    meaningful (is_meaningful=False); rows that are meaningful (True) or not yet scored
    (None) are kept, so freshly-logged events appear immediately.
    """
    q = db.query(ActivityLog).filter(ActivityLog.requisition_id == requisition_id)
    if meaningful_only:
        q = q.filter(_is_meaningful_or_unscored())
    return q.order_by(ActivityLog.created_at.desc()).limit(limit).all()


def _paginate_timeline(db: Session, *conditions, limit: int, offset: int) -> tuple[list[ActivityLog], int]:
    """Count + fetch a newest-first ActivityLog page matching ``conditions``.

    Shared tail for the per-entity timeline functions. The row fetch eager-loads a.user,
    a.company and a.vendor_card (touched per row by timeline serializers such as
    routers.activity._timeline_item) so selectinload batches each into one query instead
    of N; the .count() query stays lean and skips the eager options.
    """
    total = db.query(func.count(ActivityLog.id)).filter(*conditions).scalar() or 0
    items = (
        db.query(ActivityLog)
        .options(
            selectinload(ActivityLog.user),
            selectinload(ActivityLog.company),
            selectinload(ActivityLog.vendor_card),
        )
        .filter(*conditions)
        .order_by(ActivityLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return items, total


def get_account_timeline(
    db: Session,
    company_id: int,
    channel: list[str] | None = None,
    direction: str | None = None,
    event_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ActivityLog], int]:
    """Get filtered, paginated activity timeline for a company."""
    filters = [ActivityLog.company_id == company_id]
    if channel:
        filters.append(ActivityLog.channel.in_(channel))
    if direction:
        filters.append(ActivityLog.direction == direction)
    if event_type:
        filters.append(ActivityLog.event_type == event_type)
    if date_from:
        filters.append(ActivityLog.created_at >= date_from)
    if date_to:
        filters.append(ActivityLog.created_at <= date_to)
    return _paginate_timeline(db, *filters, limit=limit, offset=offset)


def get_vendor_timeline(
    db: Session, vendor_card_id: int, limit: int = 50, offset: int = 0
) -> tuple[list[ActivityLog], int]:
    """Paginated, eager-loaded activity timeline for a vendor card."""
    return _paginate_timeline(db, ActivityLog.vendor_card_id == vendor_card_id, limit=limit, offset=offset)


def get_user_timeline(db: Session, user_id: int, limit: int = 50, offset: int = 0) -> tuple[list[ActivityLog], int]:
    """Paginated, eager-loaded activity timeline for a user."""
    return _paginate_timeline(db, ActivityLog.user_id == user_id, limit=limit, offset=offset)


def get_contact_timeline(
    db: Session,
    site_contact_id: int,
    channel: list[str] | None = None,
    direction: str | None = None,
    event_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ActivityLog], int]:
    """Get filtered, paginated activity timeline for a site contact."""
    q = db.query(ActivityLog).filter(ActivityLog.site_contact_id == site_contact_id)
    if channel:
        q = q.filter(ActivityLog.channel.in_(channel))
    if direction:
        q = q.filter(ActivityLog.direction == direction)
    if event_type:
        q = q.filter(ActivityLog.event_type == event_type)
    if date_from:
        q = q.filter(ActivityLog.created_at >= date_from)
    if date_to:
        q = q.filter(ActivityLog.created_at <= date_to)
    total = q.count()
    items = q.order_by(ActivityLog.created_at.desc()).offset(offset).limit(limit).all()
    return items, total


def get_last_outbound_activity(db: Session, company_id: int) -> ActivityLog | None:
    """Get the most recent outbound activity for a company.

    Outbound is identified by the canonical ``direction`` column, which every
    call and email writer populates.
    """
    return (
        db.query(ActivityLog)
        .filter(
            ActivityLog.company_id == company_id,
            ActivityLog.direction == "outbound",
        )
        .order_by(ActivityLog.created_at.desc())
        .first()
    )


def days_since_last_activity(company_id: int, db: Session) -> int | None:
    """Days since last activity on a company.

    None if no activity ever.
    """
    latest = db.query(func.max(ActivityLog.created_at)).filter(ActivityLog.company_id == company_id).scalar()
    if not latest:
        return None
    delta = datetime.now(timezone.utc) - latest.replace(tzinfo=timezone.utc)
    return delta.days


_NOTE_TYPES = frozenset(
    {
        ActivityType.NOTE,
        ActivityType.SALES_NOTE,
        ActivityType.CONTACT_NOTE,
    }
)


def get_last_activity_at(company_id: int, db: Session) -> datetime | None:
    """Return the UTC datetime of the most recent non-note ActivityLog entry for a
    company.

    None if no activity ever (or only note-type entries). Notes (NOTE, SALES_NOTE,
    CONTACT_NOTE) are excluded so that a quick note does not reset the dormancy clock.
    Used by the SP4 90-day sweep to determine dormancy.

    Called by: app/services/prospect_reclamation.py
    """
    latest = (
        db.query(func.max(ActivityLog.created_at))
        .filter(
            ActivityLog.company_id == company_id,
            ActivityLog.activity_type.notin_(_NOTE_TYPES),
        )
        .scalar()
    )
    if not latest:
        return None
    if latest.tzinfo is None:
        return latest.replace(tzinfo=timezone.utc)
    return latest


# ═══════════════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _update_last_activity(match: dict, db: Session, user_id: int | None = None):
    """Update last_activity_at on the matched company or vendor.

    For companies: also updates site last_activity_at.
    """
    now = datetime.now(timezone.utc)
    if match["type"] == "company":
        db.query(Company).filter(Company.id == match["id"]).update({"last_activity_at": now}, synchronize_session=False)
        # Also update the matched site's last_activity_at
        site_id = match.get("site_id")
        if site_id:
            db.query(CustomerSite).filter(CustomerSite.id == site_id).update(
                {"last_activity_at": now}, synchronize_session=False
            )
        # Auto-claim disabled by design — ownership is always manual.
        # See ownership_service.py for the manual claim endpoint.
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
    force_meaningful: bool | None = None,
) -> ActivityLog:
    """Log a manual call against a company."""
    activity_type = ActivityType.CALL_LOGGED
    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel=Channel.PHONE,
        company_id=company_id,
        contact_phone=phone,
        contact_name=contact_name,
        duration_seconds=duration_seconds,
        direction=direction,
        notes=notes,
        is_meaningful=(
            force_meaningful
            if force_meaningful is not None
            else (duration_seconds is not None and duration_seconds >= CALL_MEANINGFUL_MIN_SECONDS)
        ),
    )
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)

    bump_company_site_activity(db, company_id, None)
    logger.info(f"Activity logged: {activity_type} -> company {company_id} by user {user_id}")
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
        activity_type=ActivityType.NOTE,
        channel=Channel.MANUAL,
        company_id=company_id,
        contact_name=contact_name,
        notes=notes,
    )
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)

    bump_company_site_activity(db, company_id, None)
    logger.info(f"Activity logged: note -> company {company_id} by user {user_id}")
    return record


# Channel → (activity_type, channel, event_type, snapshot column) for
# click-to-contact outreach logged from the CDM account workspace.
# WeChat has no dedicated snapshot column — the handle is kept in notes.
_OUTREACH_CHANNEL_MAP: dict[OutreachChannel, tuple[str, str, str, str | None]] = {
    OutreachChannel.PHONE: (ActivityType.CALL_LOGGED, Channel.PHONE, EventType.CALL, "contact_phone"),
    OutreachChannel.EMAIL: (ActivityType.EMAIL_SENT, Channel.EMAIL, EventType.EMAIL, "contact_email"),
    OutreachChannel.TEAMS: (ActivityType.TEAMS_MESSAGE, Channel.TEAMS, EventType.MESSAGE, "contact_email"),
    OutreachChannel.WECHAT: (ActivityType.WECHAT_MESSAGE, Channel.WECHAT, EventType.MESSAGE, None),
}

_OUTREACH_SUBJECT_VERBS: dict[OutreachChannel, str] = {
    OutreachChannel.PHONE: "Call to",
    OutreachChannel.EMAIL: "Email to",
    OutreachChannel.TEAMS: "Teams message to",
    OutreachChannel.WECHAT: "WeChat message to",
}

# Re-clicking the same contact link within this window returns the existing
# ActivityLog instead of writing a duplicate (double-clicks, re-opened dialers).
OUTREACH_DEDUP_SECONDS = 120


def bump_company_site_activity(db: Session, company_id: int | None, customer_site_id: int | None) -> None:
    """Set last_activity_at = now on a company and/or site (staleness sort feed)."""
    now = datetime.now(timezone.utc)
    if company_id:
        db.query(Company).filter(Company.id == company_id).update({"last_activity_at": now}, synchronize_session=False)
    if customer_site_id:
        db.query(CustomerSite).filter(CustomerSite.id == customer_site_id).update(
            {"last_activity_at": now}, synchronize_session=False
        )


def log_outreach_initiated(
    db: Session,
    *,
    user_id: int,
    channel: OutreachChannel,
    contact_value: str,
    company_id: int | None = None,
    customer_site_id: int | None = None,
    site_contact_id: int | None = None,
    contact_name: str | None = None,
    origin: str | None = None,
) -> ActivityLog:
    """Log a click-to-contact outreach event (phone/email/teams/wechat).

    Called by: POST /api/activity/outreach-initiated (CDM contact panel).
    Creates an outbound, meaningful, auto-logged ActivityLog and bumps
    last_activity_at on the company and site so staleness sorting reflects
    the touch immediately. Re-clicks within OUTREACH_DEDUP_SECONDS return the
    existing record instead of duplicating it. Caller commits.

    Raises ValueError if site_contact_id refers to a DNC contact — caller must
    convert this to a 403.
    """
    # DNC check — must be enforced before any log is written so the flag holds
    # even when the UI is bypassed (e.g. direct API calls).
    if site_contact_id:
        from ..models.crm import SiteContact

        contact = db.get(SiteContact, site_contact_id)
        if contact and contact.do_not_contact:
            raise ValueError(f"Contact {site_contact_id} is marked do-not-contact")

    if channel not in _OUTREACH_CHANNEL_MAP:
        raise ValueError(f"Unknown outreach channel: {channel}")
    activity_type, log_channel, event_type, snapshot_col = _OUTREACH_CHANNEL_MAP[channel]

    target = contact_name or contact_value
    subject = f"{_OUTREACH_SUBJECT_VERBS[channel]} {target}"

    # Dedup window — the same user re-clicking the same target within the
    # window is the same click (double-click / retry), not a second touch.
    # The key is the stable identity of the click: entity links
    # (company/site/contact, NULL-safe via SQLAlchemy `==`) plus the channel's
    # snapshot of the contacted value — NOT the display subject, so two
    # same-named contacts at one company never collapse into one log and the
    # subject wording can change without altering dedup semantics. WeChat has
    # no snapshot column; its subject embeds the handle and is deterministic
    # for an identical re-click, so it stands in as the value match.
    dedup_cutoff = datetime.now(timezone.utc) - timedelta(seconds=OUTREACH_DEDUP_SECONDS)
    value_match = (
        getattr(ActivityLog, snapshot_col) == contact_value if snapshot_col else ActivityLog.subject == subject
    )
    existing = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.user_id == user_id,
            ActivityLog.activity_type == activity_type,
            ActivityLog.channel == log_channel,
            ActivityLog.company_id == company_id,
            ActivityLog.customer_site_id == customer_site_id,
            ActivityLog.site_contact_id == site_contact_id,
            value_match,
            ActivityLog.created_at >= dedup_cutoff,
        )
        .order_by(ActivityLog.created_at.desc())
        .first()
    )
    if existing:
        logger.info(f"Outreach dedup hit: {channel} -> company {company_id} by user {user_id} (id={existing.id})")
        return existing

    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel=log_channel,
        direction=Direction.OUTBOUND,
        event_type=event_type,
        is_meaningful=True,
        auto_logged=True,
        company_id=company_id,
        customer_site_id=customer_site_id,
        site_contact_id=site_contact_id,
        contact_name=contact_name,
        subject=subject,
        notes=f"source=click_to_contact origin={origin or 'unknown'} contact={contact_value}",
    )
    if snapshot_col:
        setattr(record, snapshot_col, contact_value)
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)

    bump_company_site_activity(db, company_id, customer_site_id)
    logger.info(f"Outreach logged: {channel} -> company {company_id} by user {user_id}")
    return record


# ═══════════════════════════════════════════════════════════════════════
#  SITE-CONTACT NOTE LOGGING
# ═══════════════════════════════════════════════════════════════════════


def log_site_contact_note(
    user_id: int,
    site_contact_id: int,
    customer_site_id: int,
    company_id: int,
    notes: str,
    db: Session,
) -> ActivityLog:
    """Log a manual note against a site contact."""
    contact = db.get(SiteContact, site_contact_id)
    record = ActivityLog(
        user_id=user_id,
        activity_type=ActivityType.NOTE,
        channel=Channel.MANUAL,
        company_id=company_id,
        customer_site_id=customer_site_id,
        site_contact_id=site_contact_id,
        contact_name=contact.full_name if contact else None,
        notes=notes,
    )
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)

    bump_company_site_activity(db, company_id, customer_site_id)
    logger.info(f"Activity logged: note -> site_contact {site_contact_id} by user {user_id}")
    return record


def get_site_contact_notes(site_contact_id: int, db: Session, limit: int = 50) -> list[ActivityLog]:
    """Get recent notes for a site contact."""
    return (
        db.query(ActivityLog)
        .filter(
            ActivityLog.site_contact_id == site_contact_id,
            ActivityLog.activity_type == "note",
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .all()
    )


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
    activity_type = ActivityType.CALL_LOGGED
    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel=Channel.PHONE,
        vendor_card_id=vendor_card_id,
        vendor_contact_id=vendor_contact_id,
        contact_phone=phone,
        contact_name=contact_name,
        duration_seconds=duration_seconds,
        direction=direction,
        notes=notes,
        requisition_id=requisition_id,
        is_meaningful=(duration_seconds is not None and duration_seconds >= CALL_MEANINGFUL_MIN_SECONDS),
    )
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)

    now = datetime.now(timezone.utc)
    db.query(VendorCard).filter(VendorCard.id == vendor_card_id).update(
        {"last_activity_at": now}, synchronize_session=False
    )
    if vendor_contact_id:
        _increment_vendor_contact(vendor_contact_id, db)

    logger.info(f"Activity logged: {activity_type} -> vendor {vendor_card_id} by user {user_id}")
    return record


def log_vendor_note(
    user_id: int,
    vendor_card_id: int,
    vendor_contact_id: int | None,
    notes: str,
    contact_name: str | None,
    db: Session,
    requisition_id: int | None = None,
    bump_last_activity: bool = True,
) -> ActivityLog:
    """Log a manual note against a vendor.

    Pass bump_last_activity=False for manual add-note routes that must be cadence-
    neutral (i.e. vendor notes from the UI should not advance last_activity_at on the
    VendorCard).
    """
    record = ActivityLog(
        user_id=user_id,
        activity_type=ActivityType.NOTE,
        channel=Channel.MANUAL,
        vendor_card_id=vendor_card_id,
        vendor_contact_id=vendor_contact_id,
        contact_name=contact_name,
        notes=notes,
        requisition_id=requisition_id,
    )
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)

    if bump_last_activity:
        now = datetime.now(timezone.utc)
        db.query(VendorCard).filter(VendorCard.id == vendor_card_id).update(
            {"last_activity_at": now}, synchronize_session=False
        )
    if vendor_contact_id:
        _increment_vendor_contact(vendor_contact_id, db)

    logger.info(f"Activity logged: note -> vendor {vendor_card_id} by user {user_id}")
    return record


def get_last_call(vendor_card_id: int, db: Session) -> dict | None:
    """Get the most recent phone call activity for a vendor card.

    Returns {"user_id": int, "user_name": str, "called_at": datetime} or None.
    """
    from app.models import User

    record = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.vendor_card_id == vendor_card_id,
            ActivityLog.channel == "phone",
        )
        .order_by(ActivityLog.created_at.desc())
        .first()
    )
    if not record:
        return None

    user = db.get(User, record.user_id)
    return {
        "user_id": record.user_id,
        "user_name": user.name if user else "Unknown",
        "called_at": record.created_at.isoformat() if record.created_at else None,
    }


def days_since_last_vendor_activity(vendor_card_id: int, db: Session) -> int | None:
    """Days since last activity on a vendor card.

    None if no activity ever.
    """
    latest = db.query(func.max(ActivityLog.created_at)).filter(ActivityLog.vendor_card_id == vendor_card_id).scalar()
    if not latest:
        return None
    delta = datetime.now(timezone.utc) - latest.replace(tzinfo=timezone.utc)
    return delta.days


def _update_vendor_contact_stats(match: dict, db: Session):
    """Increment interaction_count and set last_interaction_at on matched
    VendorContact."""
    vc_id = match.get("vendor_contact_id")
    if vc_id:
        _increment_vendor_contact(vc_id, db)


def _increment_vendor_contact(vendor_contact_id: int, db: Session):
    """Increment a VendorContact's interaction stats."""
    now = datetime.now(timezone.utc)
    db.query(VendorContact).filter(VendorContact.id == vendor_contact_id).update(
        {
            "interaction_count": func.coalesce(VendorContact.interaction_count, 0) + 1,
            "last_interaction_at": now,
        },
        synchronize_session=False,
    )


# ═══════════════════════════════════════════════════════════════════════
#  RFQ / SOURCING ACTIVITY LOGGING
# ═══════════════════════════════════════════════════════════════════════


def log_activity(
    db: Session,
    *,
    activity_type: str,
    channel: str = "system",
    requisition_id: int | None = None,
    requirement_id: int | None = None,
    user_id: int | None = None,
    company_id: int | None = None,
    vendor_card_id: int | None = None,
    vendor_contact_id: int | None = None,
    buy_plan_id: int | None = None,
    description: str | None = None,
    summary: str | None = None,
    occurred_at: datetime | None = None,
    details: dict | None = None,
) -> ActivityLog:
    """Canonical writer for system/RFQ-style activity events.

    Resolves company_id from the requisition (requisition -> customer_site ->
    company) when not supplied, so the row links to both the req and its company.
    Always sets requisition_id/requirement_id so the row appears on the req
    Activity tab.

    Email and call events are written by log_email_activity()/log_call_activity(),
    which run their own contact-matching and do not route through this function.

    Called by: log_rfq_activity() (backward-compat alias). New system event
    sources (status changes, offer events, etc.) should call this directly.
    """
    if company_id is None and requisition_id:
        from ..models.crm import CustomerSite
        from ..models.sourcing import Requisition

        req = db.get(Requisition, requisition_id)
        if req is None:
            logger.warning(
                f"log_activity: requisition_id={requisition_id} not found; "
                f"activity row written without company linkage (type={activity_type})"
            )
        elif req.customer_site_id:
            site = db.get(CustomerSite, req.customer_site_id)
            if site:
                company_id = site.company_id

    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel=channel,
        requisition_id=requisition_id,
        requirement_id=requirement_id,
        company_id=company_id,
        vendor_card_id=vendor_card_id,
        vendor_contact_id=vendor_contact_id,
        buy_plan_id=buy_plan_id,
        notes=description,
        summary=summary,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        details=details,
        is_meaningful=True if activity_type in _RULE_MEANINGFUL_TYPES else None,
    )
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)

    if company_id:
        _update_last_activity({"type": "company", "id": company_id}, db)
    if vendor_card_id:
        _update_last_activity({"type": "vendor", "id": vendor_card_id}, db)

    logger.info(f"Activity logged: {activity_type} -> req {requisition_id} (channel={channel})")
    return record


def log_rfq_activity(
    db: Session,
    rfq_id: int,
    activity_type: str,
    description: str,
    metadata: dict | None = None,
    user_id: int | None = None,
    requirement_id: int | None = None,
) -> ActivityLog:
    """Backward-compatible alias for log_activity() — see that function.

    Kept so existing callers (e.g. routers/sightings.py) need no change.
    """
    return log_activity(
        db,
        activity_type=activity_type,
        channel=Channel.SYSTEM,
        requisition_id=rfq_id,
        requirement_id=requirement_id,
        user_id=user_id,
        description=description,
        details=metadata,
    )


# ═══════════════════════════════════════════════════════════════════════
#  UNMATCHED ACTIVITY QUEUE (Phase 2A)
# ═══════════════════════════════════════════════════════════════════════


def get_unmatched_activities(db: Session, limit: int = 100, offset: int = 0) -> list[ActivityLog]:
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

    logger.info(f"Activity {activity_id} attributed to {entity_type} {entity_id}")
    return activity


def dismiss_activity(activity_id: int, db: Session) -> ActivityLog | None:
    """Dismiss an unmatched activity (mark as reviewed, not useful)."""
    activity = db.get(ActivityLog, activity_id)
    if not activity:
        return None

    activity.dismissed_at = datetime.now(timezone.utc)
    db.flush()

    logger.info(f"Activity {activity_id} dismissed")
    return activity


def get_inbox_sync_status(db: Session, user) -> dict:
    """Derive inbox-sync health for the Settings card / disconnected banner.

    Reads existing User fields (no new columns). See
    app/jobs/core_jobs.py:_job_inbox_scan for the scheduled poll this surfaces. The scan
    interval resolves from the system_config DB row (admin toggle), falling back to the
    env default — so this staleness threshold matches the actual scan cadence.
    """
    from app.services.admin_service import get_effective_int
    from app.services.m365_status import action_for_reason

    now = datetime.now(timezone.utc)
    connected = bool(getattr(user, "m365_connected", False))
    last_scan = getattr(user, "last_inbox_scan", None)

    token_ok = bool(getattr(user, "access_token", None))
    exp = getattr(user, "token_expires_at", None)
    if exp is not None and _utc(exp) <= now:
        token_ok = False

    interval = get_effective_int(db, "inbox_scan_interval_min", settings.inbox_scan_interval_min)
    if last_scan is None:
        is_stale = True
    else:
        is_stale = (now - _utc(last_scan)) > timedelta(minutes=2 * interval)

    if not connected or not token_ok:
        health = InboxSyncHealth.ERROR
    elif is_stale:
        health = InboxSyncHealth.WARNING
    else:
        health = InboxSyncHealth.OK

    error_reason = getattr(user, "m365_error_reason", None)
    # Reverse-map the persisted reason to the user's next step so the card can
    # show a specific, accurate message (reconnect vs. self-healing) instead of
    # a generic "snag". None/legacy reasons resolve to a safe non-reconnect action.
    error_action = action_for_reason(error_reason)

    return {
        "connected": connected,
        "last_scan_at": _utc(last_scan) if last_scan else None,
        "is_stale": is_stale,
        "token_ok": token_ok,
        "error_reason": error_reason,
        "error_action": error_action.value if error_action else None,
        "health": health,
    }
