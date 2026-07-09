"""routers/htmx/email_views.py — Email thread viewing + reply partials (HTMX).

Sprint 7 email integration: thread viewer, AI summary, reply send, and the email
intelligence dashboard partial. Extracted verbatim from htmx_views.py (same
`/v2/partials/emails/...` + `/v2/partials/email-intelligence` paths, same
`htmx-views` tag).

Called by: app/routers/htmx_views.py (aggregated into the single exported router).
Depends on: app.models.crm (SiteContact), app.email_service, app.utils.graph_client
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_user
from ...models import SiteContact, User
from ...template_env import template_response

router = APIRouter(tags=["htmx-views"])


@router.get("/v2/partials/emails/thread/{conversation_id}", response_class=HTMLResponse)
async def email_thread_viewer(
    request: Request,
    conversation_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render email thread viewer with all messages."""
    messages = []
    error = None
    try:
        from ...dependencies import require_fresh_token as _rft

        token = await _rft(request, db)
        from ...services.email_threads import fetch_thread_messages

        messages = await fetch_thread_messages(conversation_id, token)
    except HTTPException:
        error = "M365 connection needs refresh — please reconnect in Settings"
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.error("Could not load thread: {}", exc)
        error = "Could not load thread. Please try again."

    return template_response(
        "htmx/partials/emails/thread_viewer.html",
        {"request": request, "messages": messages, "conversation_id": conversation_id, "error": error},
    )


@router.post("/v2/partials/emails/reply", response_class=HTMLResponse)
async def send_email_reply(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send an email reply and return success confirmation."""
    form = await request.form()
    to = form.get("to", "").strip()
    subject = form.get("subject", "").strip()
    body = form.get("body", "").strip()
    conversation_id = form.get("conversation_id", "").strip()

    if not to or not body:
        raise HTTPException(400, "Recipient and message body are required")

    # DNC hard-block — never email a do-not-contact recipient (checked before any
    # send attempt), mirroring send_reply_htmx / send_batch_rfq.
    dnc = (
        db.query(SiteContact)
        .filter(
            sqlfunc.lower(SiteContact.email) == to.lower(),
            SiteContact.do_not_contact.is_(True),
        )
        .first()
    )
    if dnc:
        logger.warning("Email reply skipped — do-not-contact flag set for recipient ({})", to)
        return HTMLResponse(
            '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
            "This recipient is on the do-not-contact list — reply not sent.</div>"
        )

    error = None
    try:
        from ...dependencies import require_fresh_token as _rft

        token = await _rft(request, db)
        from ...email_service import _build_html_body
        from ...utils.graph_client import GraphClient

        gc = GraphClient(token)
        html_body = _build_html_body(body)
        mail_payload = {
            "message": {
                "subject": subject or "Re:",
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": "true",
        }
        result = await gc.post_json("/me/sendMail", mail_payload)
        if "error" in result:
            error = f"Send failed: {result.get('detail', 'Unknown error')}"
    except HTTPException:
        error = "M365 connection needs refresh"
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.error("Email send failed: {}", exc)
        error = "Send failed. Please try again or contact support."

    return template_response(
        "htmx/partials/emails/reply_result.html",
        {"request": request, "to": to, "error": error, "conversation_id": conversation_id},
    )


@router.get("/v2/partials/emails/thread/{conversation_id}/summary", response_class=HTMLResponse)
async def email_thread_summary(
    request: Request,
    conversation_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return AI-generated summary of an email thread."""
    summary = None
    error = None
    try:
        from ...dependencies import require_fresh_token as _rft

        token = await _rft(request, db)
        from ...services.email_intelligence_service import summarize_thread

        summary = await summarize_thread(token, conversation_id, db, user.id)
        if not summary:
            error = "Could not generate summary"
    except HTTPException:
        error = "M365 connection needs refresh"
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.error("Summary failed: {}", exc)
        error = "Summary failed. Please try again."

    return template_response(
        "htmx/partials/emails/thread_summary.html",
        {"request": request, "summary": summary, "error": error},
    )


@router.get("/v2/partials/email-intelligence", response_class=HTMLResponse)
async def email_intelligence_partial(
    request: Request,
    classification: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return email intelligence dashboard as HTML partial."""
    from ...services.email_intelligence_service import get_recent_intelligence
    from ...services.response_analytics import get_email_intelligence_dashboard

    items = get_recent_intelligence(db, user.id, limit=50, classification=classification or None)
    dashboard = get_email_intelligence_dashboard(db, user.id, days=7)

    return template_response(
        "htmx/partials/emails/intelligence_dashboard.html",
        {"request": request, "items": items, "dashboard": dashboard, "classification": classification},
    )
