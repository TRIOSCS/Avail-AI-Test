"""routers/htmx/offers/replies.py — Reply handling / activity logging partials (HTMX +
Alpine).

Server-rendered HTML partials for vendor-response review/reply (mark reviewed/
rejected, AI-drafted reply, send-as-user) and manual communication logging (a
free-text activity note, or a logged phone call) for a requisition. Split out of
the monolithic offers.py (P4.3) along the reply-handling/logging seam.

Called by: app/routers/htmx/offers/__init__.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services.activity_service,
    app.utils.graph_client, .._shared (_base_ctx), .._shared_tabs (requisition_tab).
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ....constants import ContactStatus, VendorResponseStatus
from ....database import get_db
from ....dependencies import require_requisition_access, require_user
from ....models import Requisition, SiteContact, User
from ....models.intelligence import ActivityLog
from ....models.offers import Contact as RfqContact
from ....models.offers import VendorResponse
from ....services.activity_service import log_call_activity
from ..._lookup_helpers import get_requisition_or_404
from .._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])


@router.post("/v2/partials/requisitions/{req_id}/log-activity", response_class=HTMLResponse)
async def log_activity(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual activity (note/call/email) for a requisition."""
    from . import requisition_tab

    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

    form = await request.form()
    activity_type = form.get("activity_type", "note")
    channel_map = {"note": "note", "phone_call": "phone", "email_sent": "email"}

    log = ActivityLog(
        user_id=user.id,
        requisition_id=req_id,
        activity_type=activity_type,
        channel=channel_map.get(activity_type, "note"),
        contact_name=form.get("vendor_name", ""),
        contact_phone=form.get("contact_phone", ""),
        contact_email=form.get("contact_email", ""),
        notes=form.get("notes", ""),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    logger.info("Activity logged for req {} by {}: {}", req_id, user.email, activity_type)

    # Return refreshed activity tab
    return await requisition_tab(request=request, req_id=req_id, tab="activity", user=user, db=db)


@router.post("/v2/partials/requisitions/{req_id}/log-phone", response_class=HTMLResponse)
async def log_phone_call(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a phone call to a vendor and return updated activity tab."""
    from . import requisition_tab

    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

    form = await request.form()
    vendor_name = form.get("vendor_name", "").strip()
    vendor_phone = form.get("vendor_phone", "").strip()
    notes = form.get("notes", "").strip()

    if not vendor_name or not vendor_phone:
        raise HTTPException(400, "Vendor name and phone are required")

    contact = RfqContact(
        requisition_id=req_id,
        user_id=user.id,
        contact_type="phone",
        vendor_name=vendor_name,
        vendor_contact=vendor_phone,
        details=notes or f"Phone call to {vendor_name}",
        status=ContactStatus.SENT,
    )
    db.add(contact)

    # Route through log_call_activity so the call is matched to a vendor/company,
    # recorded as the canonical CALL_LOGGED type, and bumps last_activity_at.
    # force_meaningful=True: a manually logged call is a deliberate human interaction →
    # always meaningful, regardless of the duration-gate (which targets auto-captured
    # Teams/8x8 calls that carry a real duration).
    log = log_call_activity(
        user_id=user.id,
        direction="outbound",
        phone=vendor_phone,
        duration_seconds=None,
        external_id=None,
        contact_name=vendor_name,
        db=db,
        requisition_id=req_id,
        force_meaningful=True,
    )
    if log is not None:
        log.notes = notes or f"Called {vendor_name} at {vendor_phone}"
    db.commit()
    logger.info("Phone call logged for req {} vendor {} by {}", req_id, vendor_name, user.email)

    # Return the refreshed activity tab so the logged call appears in the timeline.
    return await requisition_tab(request=request, req_id=req_id, tab="activity", user=user, db=db)


@router.post("/v2/partials/requisitions/{req_id}/responses/{response_id}/review", response_class=HTMLResponse)
async def review_response_htmx(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a vendor response as reviewed or rejected.

    Returns updated card.
    """
    from . import template_response

    require_requisition_access(db, req_id, user)
    vr = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.id == response_id,
            VendorResponse.requisition_id == req_id,
        )
        .first()
    )
    if not vr:
        raise HTTPException(404, "Response not found")

    form = await request.form()
    status_by_action = {
        VendorResponseStatus.REVIEWED.value: VendorResponseStatus.REVIEWED,
        VendorResponseStatus.REJECTED.value: VendorResponseStatus.REJECTED,
    }
    new_status = status_by_action.get(form.get("status", ""))
    if new_status is None:
        raise HTTPException(
            400,
            f"Status must be '{VendorResponseStatus.REVIEWED.value}' or '{VendorResponseStatus.REJECTED.value}'",
        )

    vr.status = new_status
    db.commit()
    logger.info("Response {} marked as {} by {}", response_id, new_status, user.email)

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = vr
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/response_card.html", ctx)


@router.post(
    "/v2/partials/requisitions/{req_id}/responses/{response_id}/ai-draft-reply",
    response_class=HTMLResponse,
)
async def ai_draft_reply(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Draft an AI reply to a vendor response and render an editable compose block."""
    from . import template_response

    require_requisition_access(db, req_id, user)

    vr = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.id == response_id,
            VendorResponse.requisition_id == req_id,
        )
        .first()
    )
    if not vr:
        raise HTTPException(404, "Response not found")

    parsed = vr.parsed_data or {}

    from app.services.email_drafting import draft_email

    result = await draft_email(
        "vendor_reply",
        {
            "classification": vr.classification,
            "vendor_name": vr.vendor_name,
            "mpn": parsed.get("mpn"),
            "qty": parsed.get("qty") or parsed.get("qty_available"),
            "price": parsed.get("price") or parsed.get("unit_price"),
            "lead_time": parsed.get("lead_time"),
            "subject": vr.subject,
        },
    )

    default_subject = vr.subject or "RFQ"
    if not default_subject.lower().startswith("re:"):
        default_subject = f"Re: {default_subject}"

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req_id"] = req_id
    ctx["r"] = vr
    ctx["reply_subject"] = (result or {}).get("subject") or default_subject
    ctx["reply_body"] = (result or {}).get("body") or ""
    ctx["ai_failed"] = result is None
    return template_response("htmx/partials/requisitions/tabs/reply_compose.html", ctx)


@router.post(
    "/v2/partials/requisitions/{req_id}/responses/{response_id}/send-reply",
    response_class=HTMLResponse,
)
async def send_reply_htmx(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a reply to a vendor response (as the signed-in user) and mark it
    reviewed."""
    from . import template_response

    require_requisition_access(db, req_id, user)

    vr = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.id == response_id,
            VendorResponse.requisition_id == req_id,
        )
        .first()
    )
    if not vr:
        raise HTTPException(404, "Response not found")

    form = await request.form()
    subject = (form.get("subject") or "").strip() or f"Re: {vr.subject or 'RFQ'}"
    body = (form.get("body") or "").strip()
    if not body:
        raise HTTPException(400, "Reply body is required")

    # DNC hard-block — never email a do-not-contact vendor (checked in all modes).
    if vr.vendor_email:
        dnc = (
            db.query(SiteContact)
            .filter(
                sqlfunc.lower(SiteContact.email) == vr.vendor_email.lower(),
                SiteContact.do_not_contact.is_(True),
            )
            .first()
        )
        if dnc:
            return HTMLResponse(
                '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
                "This vendor is on the do-not-contact list — reply not sent.</div>"
            )

    is_testing = os.environ.get("TESTING") == "1"
    email_sent = False

    if not is_testing and vr.vendor_email:
        try:
            from ....dependencies import require_fresh_token

            token = await require_fresh_token(request, db)

            from ....utils.graph_client import GraphClient

            gc = GraphClient(token)
            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": vr.vendor_email}}],
                },
                "saveToSentItems": "true",
            }
            await gc.post_json("/me/sendMail", payload)
            email_sent = True
        except Exception as exc:
            logger.warning("Vendor reply send failed for response {}: {}", response_id, exc)

    if email_sent or is_testing:
        vr.status = VendorResponseStatus.REVIEWED
        db.commit()

    logger.info(
        "Vendor reply {} for response {} (vendor: {}) by {}",
        "sent" if email_sent or is_testing else "FAILED",
        response_id,
        vr.vendor_name,
        user.email,
    )

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = vr
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/response_card.html", ctx)


@router.patch("/v2/partials/requisitions/{req_id}/responses/{response_id}/status", response_class=HTMLResponse)
async def update_response_status(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update vendor response status (reviewed/rejected/flagged)."""
    from . import template_response

    require_requisition_access(db, req_id, user)
    vr = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.id == response_id,
            VendorResponse.requisition_id == req_id,
        )
        .first()
    )
    if not vr:
        raise HTTPException(404, "Response not found")

    form = await request.form()
    action = form.get("status", "").strip()
    # Map accepted action strings to in-vocabulary VendorResponseStatus members so
    # the persisted status always matches enum-based filters/reports.
    status_by_action = {
        VendorResponseStatus.NEW.value: VendorResponseStatus.NEW,
        VendorResponseStatus.REVIEWED.value: VendorResponseStatus.REVIEWED,
        VendorResponseStatus.REJECTED.value: VendorResponseStatus.REJECTED,
        VendorResponseStatus.FLAGGED.value: VendorResponseStatus.FLAGGED,
    }
    new_status = status_by_action.get(action)
    if new_status is None:
        raise HTTPException(
            400,
            f"Invalid status. Must be one of: {', '.join(status_by_action)}",
        )

    vr.status = new_status
    db.commit()
    logger.info("Response {} status → {} by {}", response_id, new_status, user.email)

    # Return the refreshed response card so the status control + badge update in place
    # (mirrors review_response_htmx). The card's hx-target swaps #response-{id}.
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = vr
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/response_card.html", ctx)
