"""Activity router — click-to-call background logging.

Logs phone_call activity_log entries when a user clicks a tel: link.
This is a fire-and-forget endpoint — it must never visibly fail.

Business rules:
- Rate limit: 10 calls per user per minute (Redis-backed, shared across workers)
- Invalid entity IDs are warned, not rejected
- Phone must parse to E.164 or return 400
- Entire handler wrapped in try/except — returns 201 on any internal error

Called by: app/static/app.js (logCallInitiated), app/static/crm.js
Depends on: app/utils/phone_utils.py, app/services/activity_service.py
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..constants import (
    MEANINGFUL_CALL_OUTCOMES,
    ActivityType,
    Channel,
    Direction,
    EventType,
    OutreachChannel,
)
from ..database import get_db
from ..dependencies import can_manage_account, require_user
from ..models import ActivityLog, Company, CustomerSite, SiteContact, User, VendorCard
from ..rate_limit import check_rate_limit
from ..schemas.activity import (
    CallInitiatedRequest,
    CallOutcomeRequest,
    OutreachInitiatedRequest,
)
from ..services.activity_service import bump_company_site_activity, log_outreach_initiated
from ..utils.phone_utils import format_phone_display, format_phone_e164

router = APIRouter(prefix="/api/activity", tags=["activity"])

# ── Rate-limit budgets ─────────────────────────────────────────────────
# Buckets are keyed per user AND per endpoint family so a rep's heavy phone
# use never silently blocks their email/Teams outreach logging (and vice
# versa). Outreach gets a higher budget: the CDM call-list workflow can
# legitimately produce several logs per contact per minute. The counter
# itself is the shared Redis-backed limiter in app/rate_limit.py, so these
# limits hold across worker processes and restarts (in-memory fallback when
# Redis is down).
_RATE_LIMIT = 10  # max click-to-call logs per user per minute
_OUTREACH_RATE_LIMIT = 30  # max outreach logs per user per minute


def _validated_entity_ids(
    db: Session,
    user: User,
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

    # Authorization: only attribute the activity to an account the user may act on. A
    # non-owner who clicks call/contact still gets their action logged, but it must NOT
    # bump another rep's account clocks (last_activity_at / cadence) — drop the links.
    if company_id is not None:
        company = db.get(Company, company_id)
        if company is not None and not can_manage_account(user, company, db):
            logger.warning(f"activity log: user {user.id} may not manage company {company_id} — dropping account links")
            company_id = None
            if customer_site_id is not None and "site" not in dropped:
                customer_site_id = None
                dropped.append("site")
            if site_contact_id is not None and "contact" not in dropped:
                site_contact_id = None
                dropped.append("contact")
            dropped.append("company")
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
        if not check_rate_limit(user.id, bucket="call", limit=_RATE_LIMIT):
            raise HTTPException(429, "Too many calls — try again in a minute")

        phone_display = format_phone_display(body.phone_number)

        # Resolve company from vendor if not provided
        company_id, customer_site_id, _, _ = _validated_entity_ids(db, user, body.company_id, body.customer_site_id)
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
    except Exception as e:
        logger.exception("call-initiated error")
        db.rollback()
        raise HTTPException(500, "Failed to record phone contact") from e


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

        if not check_rate_limit(user.id, bucket="outreach", limit=_OUTREACH_RATE_LIMIT):
            raise HTTPException(429, "Too many outreach logs — try again in a minute")

        company_id, customer_site_id, site_contact_id, dropped = _validated_entity_ids(
            db, user, body.company_id, body.customer_site_id, body.site_contact_id
        )

        try:
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
        except ValueError as exc:
            if "do-not-contact" in str(exc).lower() or "do not contact" in str(exc).lower():
                raise HTTPException(403, "Contact is marked do-not-contact — outreach not permitted") from exc
            raise HTTPException(400, str(exc)) from exc
        db.commit()
        # dropped_links tells the client which stale entity links were removed —
        # the touch IS logged, but it won't appear on the account it was clicked
        # from, so the frontend downgrades its success toast to a warning.
        return {"id": record.id, "dropped_links": dropped}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("outreach-initiated error")
        db.rollback()
        raise HTTPException(500, "Failed to record outreach") from e


@router.post("/{activity_id}/call-outcome", status_code=200)
def record_call_outcome(
    activity_id: int,
    body: CallOutcomeRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stamp a call outcome (connected/voicemail/no-answer/left-message) onto an
    existing ActivityLog.

    Called from the post-outreach outcome prompt in the CDM workspace. Returns 404 for
    any lookup failure to avoid existence leaks. Rate-limited under its own
    'call_outcome' bucket (separate from the outreach bucket — recording an outcome must
    not spend outreach tokens).
    """
    try:
        if not check_rate_limit(user.id, bucket="call_outcome", limit=_OUTREACH_RATE_LIMIT):
            raise HTTPException(429, "Too many requests — try again in a minute")

        record = db.get(ActivityLog, activity_id)
        if record is None or record.user_id != user.id or record.activity_type != ActivityType.CALL_LOGGED:
            raise HTTPException(404, "Activity not found")

        note = body.note.strip() if body.note else None
        patch = {**(record.details or {}), "call_outcome": body.outcome.value}
        if note:
            patch["outcome_note"] = note
        record.details = patch
        flag_modified(record, "details")

        if note:
            existing = record.notes or ""
            record.notes = (existing + "\n" + note).strip()

        record.is_meaningful = body.outcome in MEANINGFUL_CALL_OUTCOMES

        db.commit()
        return {"ok": True, "outcome": body.outcome.value}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("call-outcome error")
        db.rollback()
        raise HTTPException(500, "Failed to record call outcome") from e
