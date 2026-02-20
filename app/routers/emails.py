"""
routers/emails.py — Email thread viewing and reply endpoints

Surfaces vendor email threads on requirement and vendor detail views.
Uses Graph API to fetch threads on demand (never stores email bodies).

Business Rules:
- All endpoints require a valid M365 token (require_fresh_token)
- Threads are cached in-memory for 5 minutes
- Internal TRIOSCS-to-TRIOSCS emails are filtered out
- Reply uses existing Graph API send capability

Called by: main.py (router mount)
Depends on: services/email_threads.py, email_service.py, dependencies.py
"""

from loguru import logger

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_fresh_token, require_user
from ..models import User
from ..schemas.emails import (
    EmailReplyRequest,
    EmailThreadListResponse,
    EmailThreadMessagesResponse,
)
from ..services.email_threads import (
    fetch_thread_messages,
    fetch_threads_for_requirement,
    fetch_threads_for_vendor,
)

router = APIRouter(tags=["emails"])


@router.get("/api/requirements/{requirement_id}/emails")
async def list_requirement_emails(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List email threads linked to a requirement."""
    try:
        token = await require_fresh_token(request, db)
    except HTTPException:
        return EmailThreadListResponse(
            threads=[],
            error="M365 connection needs refresh — please reconnect in Settings",
        ).model_dump()

    try:
        threads = await fetch_threads_for_requirement(
            requirement_id, token, db, user_id=user.id
        )
        return EmailThreadListResponse(threads=threads).model_dump()
    except Exception as e:
        logger.error(f"Failed to fetch threads for requirement {requirement_id}: {e}")
        return EmailThreadListResponse(
            threads=[],
            error="Could not load emails — please try again",
        ).model_dump()


@router.get("/api/emails/thread/{conversation_id}")
async def get_thread_messages(
    conversation_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get all messages in a conversation thread."""
    try:
        token = await require_fresh_token(request, db)
    except HTTPException:
        return EmailThreadMessagesResponse(
            messages=[],
            error="M365 connection needs refresh",
        ).model_dump()

    try:
        messages = await fetch_thread_messages(conversation_id, token)
        return EmailThreadMessagesResponse(messages=messages).model_dump()
    except Exception as e:
        logger.error(f"Failed to fetch thread messages: {e}")
        return EmailThreadMessagesResponse(
            messages=[],
            error="Could not load thread messages",
        ).model_dump()


@router.get("/api/vendors/{vendor_card_id}/emails")
async def list_vendor_emails(
    vendor_card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List email threads with a vendor."""
    try:
        token = await require_fresh_token(request, db)
    except HTTPException:
        return EmailThreadListResponse(
            threads=[],
            error="M365 connection needs refresh — please reconnect in Settings",
        ).model_dump()

    try:
        threads = await fetch_threads_for_vendor(
            vendor_card_id, token, db, user_id=user.id
        )
        return EmailThreadListResponse(threads=threads).model_dump()
    except Exception as e:
        logger.error(f"Failed to fetch threads for vendor {vendor_card_id}: {e}")
        return EmailThreadListResponse(
            threads=[],
            error="Could not load emails — please try again",
        ).model_dump()


@router.post("/api/emails/reply")
async def send_reply(
    payload: EmailReplyRequest,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a reply in an existing email thread."""
    try:
        token = await require_fresh_token(request, db)
    except HTTPException:
        raise HTTPException(401, "M365 connection needs refresh")

    from ..email_service import _build_html_body
    from ..utils.graph_client import GraphClient

    gc = GraphClient(token)
    html_body = _build_html_body(payload.body)

    # Build the reply payload
    mail_payload = {
        "message": {
            "subject": payload.subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": payload.to}}],
        },
        "saveToSentItems": "true",
    }

    try:
        result = await gc.post_json("/me/sendMail", mail_payload)
        if "error" in result:
            raise HTTPException(502, f"Failed to send reply: {result.get('detail', '')}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reply send failed: {e}")
        raise HTTPException(502, f"Failed to send reply: {str(e)[:200]}")

    # Invalidate cache for threads involving this conversation
    from ..services.email_threads import clear_cache
    clear_cache()

    return {"ok": True, "message": f"Reply sent to {payload.to}"}
