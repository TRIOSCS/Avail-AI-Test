"""Activity router — click-to-call background logging.

Logs phone_call activity_log entries when a user clicks a tel: link.
This is a fire-and-forget endpoint — it must never visibly fail.

Business rules:
- Rate limit: 10 calls per user per minute (in-memory)
- Invalid entity IDs are warned, not rejected
- Phone must parse to E.164 or return 400
- Entire handler wrapped in try/except — returns 201 on any internal error

Called by: app/static/app.js (logCallInitiated), app/static/crm.js
Depends on: app/utils/phone_utils.py, app/services/activity_service.py
"""

import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType, Channel, Direction, EventType, OutreachChannel
from ..database import get_db
from ..dependencies import require_user
from ..models import ActivityLog, Company, CustomerSite, SiteContact, User, VendorCard
from ..schemas.activity import ActivityTimelineResponse, CallInitiatedRequest, OutreachInitiatedRequest
from ..services.activity_service import bump_company_site_activity, log_outreach_initiated
from ..utils.phone_utils import format_phone_display, format_phone_e164

router = APIRouter(prefix="/api/activity", tags=["activity"])

# ── In-memory rate limiter ─────────────────────────────────────────────
# Buckets are keyed per user AND per endpoint family so a rep's heavy phone
# use never silently blocks their email/Teams outreach logging (and vice
# versa). Outreach gets a higher budget: the CDM call-list workflow can
# legitimately produce several logs per contact per minute.
_call_log: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 10  # max click-to-call logs per user per minute
_OUTREACH_RATE_LIMIT = 30  # max outreach logs per user per minute


def _check_rate_limit(user_id: int, bucket: str = "call", limit: int = _RATE_LIMIT) -> bool:
    """Return True if user is within rate limit, False if exceeded."""
    key = f"{user_id}:{bucket}"
    now = time.time()
    window = now - 60
    timestamps = _call_log[key]
    # Prune old entries
    _call_log[key] = [t for t in timestamps if t > window]
    if len(_call_log[key]) >= limit:
        return False
    _call_log[key].append(now)
    return True


def _validated_entity_ids(
    db: Session,
    company_id: int | None,
    customer_site_id: int | None,
    site_contact_id: int | None = None,
) -> tuple[int | None, int | None, int | None, list[str]]:
    """Drop entity links that don't exist or don't belong together (warn, don't reject).

    The DOM can carry stale ids (e.g. a coworker deleted or moved the contact while the
    panel was open). Nonexistent ids would raise FK IntegrityError → opaque 500, and
    ActivityLog has no DB constraint tying company/site/contact together, so a
    mismatched triple would persist an inconsistent link and bump last_activity_at on an
    unrelated site. Keep the activity, drop the dangling/mismatched links, and report
    what was dropped so the caller can surface the degradation instead of claiming full
    success.

    Returns (company_id, customer_site_id, site_contact_id, dropped) — dropped is a
    subset of ["company", "site", "contact"] naming the links removed.
    """
    dropped: list[str] = []
    if company_id and not db.get(Company, company_id):
        logger.warning(f"activity log: company_id={company_id} not found — dropping link")
        company_id = None
        dropped.append("company")
    site = db.get(CustomerSite, customer_site_id) if customer_site_id else None
    if customer_site_id and not site:
        logger.warning(f"activity log: customer_site_id={customer_site_id} not found — dropping link")
        customer_site_id = None
        dropped.append("site")
    elif site and site.company_id != company_id:
        logger.warning(
            f"activity log: customer_site_id={customer_site_id} belongs to company "
            f"{site.company_id}, not {company_id} — dropping link"
        )
        customer_site_id = None
        dropped.append("site")
    contact = db.get(SiteContact, site_contact_id) if site_contact_id else None
    if site_contact_id and not contact:
        logger.warning(f"activity log: site_contact_id={site_contact_id} not found — dropping link")
        site_contact_id = None
        dropped.append("contact")
    elif contact and contact.customer_site_id != customer_site_id:
        logger.warning(
            f"activity log: site_contact_id={site_contact_id} belongs to site "
            f"{contact.customer_site_id}, not {customer_site_id} — dropping link"
        )
        site_contact_id = None
        dropped.append("contact")
    return company_id, customer_site_id, site_contact_id, dropped


@router.post("/call-initiated", status_code=201)
def call_initiated(
    body: CallInitiatedRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a click-to-call event.

    Must never visibly fail.
    """
    try:
        # Validate phone
        e164 = format_phone_e164(body.phone_number)
        if not e164:
            raise HTTPException(400, "Invalid phone number")

        # Rate limit
        if not _check_rate_limit(user.id):
            raise HTTPException(429, "Too many calls — try again in a minute")

        phone_display = format_phone_display(body.phone_number)

        # Resolve company from vendor if not provided
        company_id, customer_site_id, _, _ = _validated_entity_ids(db, body.company_id, body.customer_site_id)
        vendor_card_id = body.vendor_card_id
        vendor_name = None

        if vendor_card_id:
            vendor = db.get(VendorCard, vendor_card_id)
            if vendor:
                vendor_name = vendor.display_name
                # VendorCard has no direct company FK — company resolved by phone matching
            else:
                logger.warning(f"call-initiated: vendor_card_id={vendor_card_id} not found")

        # Resolve requisition_id from requirement_id
        requisition_id = None
        if body.requirement_id:
            from ..models.sourcing import Requirement

            req = db.get(Requirement, body.requirement_id)
            if req:
                requisition_id = req.requisition_id
            else:
                logger.warning(f"call-initiated: requirement_id={body.requirement_id} not found")

        # Build subject
        if vendor_name:
            subject = f"Call to {vendor_name}"
        else:
            subject = f"Call to {phone_display}"

        # Create activity_log entry
        record = ActivityLog(
            user_id=user.id,
            activity_type=ActivityType.CALL_LOGGED,
            channel=Channel.PHONE,
            direction=Direction.OUTBOUND,
            event_type=EventType.CALL,
            is_meaningful=True,
            vendor_card_id=vendor_card_id,
            company_id=company_id,
            customer_site_id=customer_site_id,
            requisition_id=requisition_id,
            contact_phone=e164,
            subject=subject,
            auto_logged=True,
            notes=f"source=click_to_call origin={body.origin or 'unknown'} phone_display={phone_display}",
        )
        db.add(record)
        # A logged call is a touch — keep the CDM staleness sort honest.
        bump_company_site_activity(db, company_id, customer_site_id)
        db.commit()

        return {"id": record.id}

    except HTTPException:
        raise
    except Exception:
        logger.exception("call-initiated error")
        db.rollback()
        raise HTTPException(500, "Failed to record phone contact")


@router.post("/outreach-initiated", status_code=201)
def outreach_initiated(
    body: OutreachInitiatedRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a click-to-contact event (phone / email / Teams / WeChat).

    Fired from the CDM contact panel on tel:/mailto:/Teams/WeChat link clicks.
    Creates an outbound ActivityLog and bumps company/site last_activity_at so
    the staleness sort updates. Re-clicks within the dedup window return the
    existing record.
    """
    try:
        contact_value = body.contact_value.strip()
        if body.channel == OutreachChannel.PHONE:
            e164 = format_phone_e164(contact_value)
            if not e164:
                raise HTTPException(400, "Invalid phone number")
            contact_value = e164
        elif body.channel in (OutreachChannel.EMAIL, OutreachChannel.TEAMS):
            local, _, domain = contact_value.partition("@")
            if not local or "." not in domain:
                raise HTTPException(400, "Invalid email address")
        elif not contact_value:
            raise HTTPException(400, "Contact value is required")

        if not _check_rate_limit(user.id, bucket="outreach", limit=_OUTREACH_RATE_LIMIT):
            raise HTTPException(429, "Too many outreach logs — try again in a minute")

        company_id, customer_site_id, site_contact_id, dropped = _validated_entity_ids(
            db, body.company_id, body.customer_site_id, body.site_contact_id
        )

        record = log_outreach_initiated(
            db,
            user_id=user.id,
            channel=body.channel,
            contact_value=contact_value,
            company_id=company_id,
            customer_site_id=customer_site_id,
            site_contact_id=site_contact_id,
            contact_name=body.contact_name,
            origin=body.origin,
        )
        db.commit()
        # dropped_links tells the client which stale entity links were removed —
        # the touch IS logged, but it won't appear on the account it was clicked
        # from, so the frontend downgrades its success toast to a warning.
        return {"id": record.id, "dropped_links": dropped}

    except HTTPException:
        raise
    except Exception:
        logger.exception("outreach-initiated error")
        db.rollback()
        raise HTTPException(500, "Failed to record outreach")


@router.get("/account/{company_id}", response_model=ActivityTimelineResponse)
def get_account_timeline_endpoint(
    company_id: int,
    channel: list[str] | None = Query(default=None),
    direction: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get paginated activity timeline for a company account."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    from datetime import datetime as dt

    from ..services.activity_service import get_account_timeline

    try:
        df = dt.fromisoformat(date_from) if date_from else None
        dto = dt.fromisoformat(date_to) if date_to else None
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid date format — expected ISO 8601")

    items, total = get_account_timeline(
        db,
        company_id,
        channel=channel,
        direction=direction,
        event_type=event_type,
        date_from=df,
        date_to=dto,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [_timeline_item(a) for a in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/contact/{site_contact_id}", response_model=ActivityTimelineResponse)
def get_contact_timeline_endpoint(
    site_contact_id: int,
    channel: list[str] | None = Query(default=None),
    direction: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get paginated activity timeline for a site contact."""
    from ..models import SiteContact
    from ..services.activity_service import get_contact_timeline

    contact = db.get(SiteContact, site_contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")

    from datetime import datetime as dt

    try:
        df = dt.fromisoformat(date_from) if date_from else None
        dto = dt.fromisoformat(date_to) if date_to else None
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid date format — expected ISO 8601")

    items, total = get_contact_timeline(
        db,
        site_contact_id,
        channel=channel,
        direction=direction,
        event_type=event_type,
        date_from=df,
        date_to=dto,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [_timeline_item(a) for a in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _timeline_item(a: ActivityLog) -> dict:
    """Serialize an ActivityLog for timeline responses."""
    return {
        "id": a.id,
        "user_id": a.user_id,
        "user_name": a.user.name if a.user else None,
        "activity_type": a.activity_type,
        "channel": a.channel,
        "company_id": a.company_id,
        "company_name": a.company.name if a.company else None,
        "vendor_card_id": a.vendor_card_id,
        "vendor_name": a.vendor_card.display_name if a.vendor_card else None,
        "vendor_contact_id": getattr(a, "vendor_contact_id", None),
        "site_contact_id": getattr(a, "site_contact_id", None),
        "contact_email": a.contact_email,
        "contact_phone": a.contact_phone,
        "contact_name": a.contact_name,
        "subject": a.subject,
        "notes": getattr(a, "notes", None),
        "duration_seconds": a.duration_seconds,
        "direction": getattr(a, "direction", None),
        "event_type": getattr(a, "event_type", None),
        "summary": getattr(a, "summary", None),
        "source_url": getattr(a, "source_url", None),
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.get("/vendors/{vendor_card_id}/last-call")
def get_vendor_last_call(
    vendor_card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get the most recent phone call for a vendor card."""
    from ..services.activity_service import get_last_call

    result = get_last_call(vendor_card_id, db)
    if not result:
        return {"last_call": None}

    return {"last_call": result, "is_current_user": result["user_id"] == user.id}
