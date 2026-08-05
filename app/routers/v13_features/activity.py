"""activity.py — Activity-related v1.3.0 routes.

Graph/Teams/ACS webhooks and the vendor activity timeline.

Called by: v13_features package __init__.py
Depends on: services/activity_service, services/webhook_service
"""

import hmac

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...config import settings
from ...database import get_db
from ...dependencies import require_user
from ...models import User
from ...rate_limit import limiter
from ...schemas.v13_features import GraphWebhookPayload

router = APIRouter(tags=["v13"])


# ═══════════════════════════════════════════════════════════════════════
#  GRAPH WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════


def _validation_echo_response(validation_token: str) -> PlainTextResponse:
    """Echo Graph's subscription-validation token under strict bounds.

    Graph REQUIRES the raw ``validationToken`` returned with HTTP 200 as
    text/plain on subscription creation, so we must mirror it. To keep this
    unauthenticated echo from reflecting oversized or HTML/script payloads we
    (1) bound length + charset via ``is_safe_validation_token`` (reject with
    400 otherwise) and (2) pin an explicit ``text/plain; charset=utf-8`` body
    with ``X-Content-Type-Options: nosniff`` so no browser will content-sniff
    it into HTML.
    """
    from app.services.webhook_service import is_safe_validation_token

    if not is_safe_validation_token(validation_token):
        raise HTTPException(400, "Invalid validation token")
    return PlainTextResponse(
        content=validation_token,
        status_code=200,
        media_type="text/plain; charset=utf-8",
        headers={"X-Content-Type-Options": "nosniff"},
    )


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
        return _validation_echo_response(validation_token)

    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(400, "Invalid JSON payload") from e

    payload = GraphWebhookPayload.model_validate(raw)

    from app.services.webhook_service import handle_notification, validate_notifications

    payload_dict = payload.model_dump()
    validated = validate_notifications(payload_dict, db)
    if not validated:
        raise HTTPException(403, "No valid notifications")

    try:
        await handle_notification(payload_dict, db, validated=validated)
    except Exception as e:
        logger.exception("Webhook notification processing failed")
        raise HTTPException(500, "Processing failed") from e
    return {"status": "accepted"}


@router.post("/api/webhooks/teams")
@limiter.limit("600/minute")
async def teams_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Microsoft Graph Teams webhook endpoint.

    Handles validation handshake and Teams chat message notifications.
    """
    if settings.mvp_mode:
        raise HTTPException(404, "Teams tracking not available in MVP mode")

    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return _validation_echo_response(validation_token)

    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(400, "Invalid JSON payload") from e

    from app.services.webhook_service import handle_teams_notification, validate_notifications

    validated = validate_notifications(raw, db)
    if not validated:
        raise HTTPException(403, "No valid notifications")

    try:
        await handle_teams_notification(raw, db, validated=validated)
    except Exception as e:
        logger.exception("Teams webhook notification processing failed")
        raise HTTPException(500, "Processing failed") from e
    return {"status": "accepted"}


# ═══════════════════════════════════════════════════════════════════════
#  ACS WEBHOOKS (Azure Communication Services)
# ═══════════════════════════════════════════════════════════════════════


@router.post("/api/webhooks/acs")
@limiter.limit("120/minute")
async def acs_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Azure Communication Services webhook — logs completed calls.

    Validates that ACS is configured, authenticates the request via a shared
    secret carried in the ``?secret=`` query param (Event Grid has no
    clientState-style body field like Graph, so the secret travels in the
    URL baked into the subscription's webhook endpoint — see
    ``settings.acs_webhook_secret``), then checks for EventGrid validation
    events.
    """
    if not settings.acs_connection_string:
        raise HTTPException(503, "ACS not configured")

    # Fail closed: an unconfigured secret means we can never trust a caller,
    # including the Event Grid subscription-validation handshake itself.
    if not settings.acs_webhook_secret:
        logger.warning("ACS webhook secret not configured; rejecting event")
        raise HTTPException(403, "Webhook not authorized")

    provided_secret = request.query_params.get("secret", "")
    if not hmac.compare_digest(provided_secret, settings.acs_webhook_secret):
        logger.warning("ACS webhook secret mismatch; rejecting event")
        raise HTTPException(403, "Webhook not authorized")

    try:
        events = await request.json()
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(400, "Invalid JSON") from e

    # Handle EventGrid subscription validation handshake
    if isinstance(events, list) and len(events) == 1:
        first = events[0]
        if first.get("eventType") == "Microsoft.EventGrid.SubscriptionValidationEvent":
            validation_code = first.get("data", {}).get("validationCode")
            if validation_code:
                return {"validationResponse": validation_code}

    if isinstance(events, list):
        for event in events:
            event_type = event.get("type", "")
            if "CallCompleted" in event_type or "CallDisconnected" in event_type:
                from app.services.acs_service import handle_call_completed
                from app.services.activity_service import log_call_activity

                call_data = handle_call_completed(event.get("data", {}))
                if call_data:
                    log_call_activity(
                        user_id=None,
                        direction=call_data["direction"],
                        phone=call_data["to_phone"],
                        duration_seconds=call_data["duration_seconds"],
                        external_id=call_data["call_connection_id"],
                        contact_name=None,
                        db=db,
                    )
        db.commit()

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


@router.get("/api/vendors/{vendor_id}/activities")
async def get_vendor_activities(
    vendor_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get the paginated activity timeline for a vendor."""
    from app.services.activity_service import get_vendor_timeline

    items, total = get_vendor_timeline(db, vendor_id, limit=limit, offset=offset)
    return {"items": [_activity_to_dict(a) for a in items], "total": total, "limit": limit, "offset": offset}
