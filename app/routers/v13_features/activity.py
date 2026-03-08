"""
activity.py — Activity-related v1.3.0 routes

Graph webhooks, activity logging (email, call, note), unmatched queue,
activity attribution/dismissal, and activity-status indicators.

Called by: v13_features package __init__.py
Depends on: services/activity_service, services/webhook_service
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...config import settings
from ...database import get_db
from ...dependencies import require_admin, require_user
from ...models import Company, User, VendorCard
from ...rate_limit import limiter
from ...schemas.v13_features import (
    ActivityAttributeRequest,
    CompanyCallLog,
    CompanyNoteLog,
    EmailClickLog,
    GraphWebhookPayload,
    PhoneCallLog,
    VendorCallLog,
    VendorNoteLog,
)

router = APIRouter(tags=["v13"])


# ═══════════════════════════════════════════════════════════════════════
#  GRAPH WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════


@router.post("/api/webhooks/graph")
@limiter.limit("60/minute")
async def graph_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Microsoft Graph webhook endpoint.

    Handles validation handshake and notification payloads.
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return PlainTextResponse(content=validation_token, status_code=200)

    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON payload")

    payload = GraphWebhookPayload.model_validate(raw)

    from app.services.webhook_service import handle_notification, validate_notifications

    payload_dict = payload.model_dump()
    validated = validate_notifications(payload_dict, db)
    if not validated:
        raise HTTPException(403, "No valid notifications")

    try:
        await handle_notification(payload_dict, db, validated=validated)
    except Exception:
        logger.exception("Webhook notification processing failed")
        raise HTTPException(502, "Notification processing failed")
    return {"status": "accepted"}


# ═══════════════════════════════════════════════════════════════════════
#  ACTIVITY LOG
# ═══════════════════════════════════════════════════════════════════════


def _activity_to_dict(a) -> dict:
    """Serialize an ActivityLog record."""
    return {
        "id": a.id,
        "user_id": a.user_id,
        "user_name": a.user.name if a.user else None,
        "activity_type": a.activity_type,
        "channel": a.channel,
        "company_id": a.company_id,
        "vendor_card_id": a.vendor_card_id,
        "vendor_contact_id": getattr(a, "vendor_contact_id", None),
        "site_contact_id": getattr(a, "site_contact_id", None),
        "contact_email": a.contact_email,
        "contact_phone": a.contact_phone,
        "contact_name": a.contact_name,
        "subject": a.subject,
        "notes": getattr(a, "notes", None),
        "duration_seconds": a.duration_seconds,
        "requisition_id": getattr(a, "requisition_id", None),
        "direction": getattr(a, "direction", None),
        "event_type": getattr(a, "event_type", None),
        "summary": getattr(a, "summary", None),
        "source_url": getattr(a, "source_url", None),
        "dismissed_at": a.dismissed_at.isoformat() if getattr(a, "dismissed_at", None) else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.get("/api/companies/{company_id}/activities")
async def get_company_activities(company_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get activity log for a company."""
    from app.services.activity_service import get_company_activities as _get

    activities = _get(company_id, db)
    return [_activity_to_dict(a) for a in activities]


@router.post("/api/companies/{company_id}/activities/call")
async def log_company_phone_call(
    company_id: int,
    payload: CompanyCallLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual phone call against a company."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    from app.services.activity_service import log_company_call

    record = log_company_call(
        user_id=user.id,
        company_id=company_id,
        direction=payload.direction,
        phone=payload.phone,
        duration_seconds=payload.duration_seconds,
        contact_name=payload.contact_name,
        notes=payload.notes,
        db=db,
    )
    db.commit()
    return {"status": "logged", "activity_id": record.id}


@router.post("/api/companies/{company_id}/activities/note")
async def log_company_note_endpoint(
    company_id: int,
    payload: CompanyNoteLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual note against a company."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    from app.services.activity_service import log_company_note

    record = log_company_note(
        user_id=user.id,
        company_id=company_id,
        contact_name=payload.contact_name,
        notes=payload.notes,
        db=db,
    )
    db.commit()
    return {"status": "logged", "activity_id": record.id}


@router.get("/api/vendors/{vendor_id}/activities")
async def get_vendor_activities(vendor_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get activity log for a vendor."""
    from app.services.activity_service import get_vendor_activities as _get

    activities = _get(vendor_id, db)
    return [_activity_to_dict(a) for a in activities]


@router.get("/api/users/{target_user_id}/activities")
async def get_user_activities(
    target_user_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get activity log for a specific user."""
    from app.services.activity_service import get_user_activities as _get

    activities = _get(target_user_id, db)
    return [_activity_to_dict(a) for a in activities]


@router.post("/api/activities/email")
async def log_email_click(
    body: EmailClickLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Auto-log when a mailto: link is clicked anywhere in the app."""
    from app.services.activity_service import log_email_activity

    email = body.email.strip()
    if not email:
        return {"status": "skipped", "message": "No email provided"}
    record = log_email_activity(
        user_id=user.id,
        direction="sent",
        email_addr=email,
        subject=None,
        external_id=None,
        contact_name=body.contact_name,
        db=db,
    )
    db.commit()
    if record:
        return {"status": "logged", "activity_id": record.id}
    return {"status": "no_match", "message": "Email did not match any known contact"}


@router.post("/api/activities/call")
async def log_phone_call(
    payload: PhoneCallLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a phone call (click-to-call or softphone integration)."""
    from app.services.activity_service import log_call_activity

    record = log_call_activity(
        user_id=user.id,
        direction=payload.direction,
        phone=payload.phone,
        duration_seconds=payload.duration_seconds,
        external_id=payload.external_id,
        contact_name=payload.contact_name,
        db=db,
    )
    db.commit()
    if record:
        return {"status": "logged", "activity_id": record.id}
    return {
        "status": "no_match",
        "message": "Phone number did not match any known contact",
    }


@router.post("/api/vendors/{vendor_id}/activities/call")
async def log_vendor_phone_call(
    vendor_id: int,
    payload: VendorCallLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual phone call against a vendor card."""
    card = db.get(VendorCard, vendor_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    from app.services.activity_service import log_vendor_call

    record = log_vendor_call(
        user_id=user.id,
        vendor_card_id=vendor_id,
        vendor_contact_id=payload.vendor_contact_id,
        direction=payload.direction,
        phone=payload.phone,
        duration_seconds=payload.duration_seconds,
        contact_name=payload.contact_name,
        notes=payload.notes,
        db=db,
        requisition_id=payload.requisition_id,
    )
    db.commit()
    return {"status": "logged", "activity_id": record.id}


@router.post("/api/vendors/{vendor_id}/activities/note")
async def log_vendor_note_endpoint(
    vendor_id: int,
    payload: VendorNoteLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual note against a vendor card."""
    card = db.get(VendorCard, vendor_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    from app.services.activity_service import log_vendor_note

    record = log_vendor_note(
        user_id=user.id,
        vendor_card_id=vendor_id,
        vendor_contact_id=payload.vendor_contact_id,
        notes=payload.notes,
        contact_name=payload.contact_name,
        db=db,
        requisition_id=payload.requisition_id,
    )
    db.commit()
    return {"status": "logged", "activity_id": record.id}


# ═══════════════════════════════════════════════════════════════════════
#  UNMATCHED ACTIVITY QUEUE (Phase 2A)
# ═══════════════════════════════════════════════════════════════════════


@router.get("/api/activities/unmatched")
async def list_unmatched_activities(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List activities with no company or vendor match (admin only)."""
    from app.services.activity_service import (
        count_unmatched_activities,
        get_unmatched_activities,
    )

    activities = get_unmatched_activities(db, limit=limit, offset=offset)
    total = count_unmatched_activities(db)
    return {
        "items": [_activity_to_dict(a) for a in activities],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/api/activities/{activity_id}/attribute")
async def attribute_activity_endpoint(
    activity_id: int,
    payload: ActivityAttributeRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Attribute an unmatched activity to a company or vendor (admin only)."""
    # Verify the target entity exists
    if payload.entity_type == "company":
        target = db.get(Company, payload.entity_id)
        if not target:
            raise HTTPException(404, "Company not found")
    elif payload.entity_type == "vendor":
        target = db.get(VendorCard, payload.entity_id)
        if not target:
            raise HTTPException(404, "Vendor not found")

    from app.services.activity_service import attribute_activity

    result = attribute_activity(
        activity_id=activity_id,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        db=db,
        user_id=user.id,
    )
    if not result:
        raise HTTPException(404, "Activity not found")

    db.commit()
    return {"status": "attributed", "activity": _activity_to_dict(result)}


@router.post("/api/activities/{activity_id}/dismiss")
async def dismiss_activity_endpoint(
    activity_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Dismiss an unmatched activity (admin only)."""
    from app.services.activity_service import dismiss_activity

    result = dismiss_activity(activity_id, db)
    if not result:
        raise HTTPException(404, "Activity not found")

    db.commit()
    return {"status": "dismissed", "activity_id": activity_id}


@router.get("/api/vendors/{vendor_id}/activity-status")
async def vendor_activity_status(
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get activity health status for a vendor (green/yellow/red indicator)."""
    from app.services.activity_service import days_since_last_vendor_activity

    card = db.get(VendorCard, vendor_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    days = days_since_last_vendor_activity(vendor_id, db)

    if days is None:
        status = "no_activity"
    elif days <= settings.customer_warning_days:
        status = "green"
    elif days <= settings.vendor_protection_warn_days:
        status = "yellow"
    else:
        status = "red"

    return {
        "vendor_card_id": vendor_id,
        "days_since_activity": days,
        "status": status,
    }


@router.get("/api/companies/{company_id}/activity-status")
async def company_activity_status(company_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get activity health status for a company (for dashboard indicators)."""
    from app.config import settings as cfg
    from app.services.activity_service import days_since_last_activity

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    days = days_since_last_activity(company_id, db)
    inactivity_limit = cfg.strategic_inactivity_days if company.is_strategic else cfg.customer_inactivity_days

    if days is None:
        status = "no_activity"
    elif days <= cfg.customer_warning_days:
        status = "green"
    elif days <= inactivity_limit:
        status = "yellow"
    else:
        status = "red"

    return {
        "company_id": company_id,
        "days_since_activity": days,
        "inactivity_limit": inactivity_limit,
        "is_strategic": company.is_strategic or False,
        "status": status,
        "account_owner_id": company.account_owner_id,
    }
