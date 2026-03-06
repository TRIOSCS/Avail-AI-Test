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

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import ActivityLog, User, VendorCard
from ..schemas.activity import CallInitiatedRequest
from ..utils.phone_utils import format_phone_display, format_phone_e164

router = APIRouter(prefix="/api/activity", tags=["activity"])

# ── In-memory rate limiter ─────────────────────────────────────────────
_call_log: dict[int, list[float]] = defaultdict(list)
_RATE_LIMIT = 10  # max calls per user per minute


def _check_rate_limit(user_id: int) -> bool:
    """Return True if user is within rate limit, False if exceeded."""
    now = time.time()
    window = now - 60
    timestamps = _call_log[user_id]
    # Prune old entries
    _call_log[user_id] = [t for t in timestamps if t > window]
    if len(_call_log[user_id]) >= _RATE_LIMIT:
        return False
    _call_log[user_id].append(now)
    return True


@router.post("/call-initiated", status_code=201)
def call_initiated(
    body: CallInitiatedRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a click-to-call event. Must never visibly fail."""
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
        company_id = body.company_id
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
            activity_type="phone_call",
            channel="phone",
            vendor_card_id=vendor_card_id,
            company_id=company_id,
            customer_site_id=body.customer_site_id,
            requisition_id=requisition_id,
            contact_phone=e164,
            subject=subject,
            auto_logged=True,
            notes=f"source=click_to_call origin={body.origin or 'unknown'} phone_display={phone_display}",
        )
        db.add(record)
        db.commit()

        return {"id": record.id}

    except HTTPException:
        raise  # Let 400/429 propagate
    except Exception as e:
        logger.error(f"call-initiated error (swallowed): {e}")
        db.rollback()
        return {"id": None}


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
