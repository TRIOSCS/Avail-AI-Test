"""routers/htmx/offers/follow_ups.py — Follow-up scheduling/delivery/AI drafting (HTMX +
Alpine).

Server-rendered HTML partials for the cross-requisition follow-up queue: the
list partial, single-contact send, AI-drafted follow-up body, the send-all batch
(with an honest per-contact tally + toast), and the nav sidebar badge. Split out
of the monolithic offers.py (P4.3) along the follow-up seam.

Called by: app/routers/htmx/offers/__init__.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.config, app.utils.graph_client,
    .._shared (_base_ctx).
"""

import json
import os
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ....config import settings
from ....constants import RESTRICTED_ROLES, ContactStatus, UserRole
from ....database import get_db
from ....dependencies import require_requisition_access, require_user
from ....models import Requisition, SiteContact, User
from ....models.offers import Contact as RfqContact
from .._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])


def _build_follow_ups_ctx(request: Request, user: User, db: Session) -> dict:
    """Build the cross-requisition follow-up queue template context.

    Shared by the list partial and the batch-send re-render so both surfaces render the
    SAME queue (same threshold, same per-owner scope). Extracted so send-batch can
    return the refreshed list instead of a bare success div that replaced the whole
    page.
    """
    threshold = datetime.now(UTC) - timedelta(days=settings.follow_up_days)

    stale_q = db.query(RfqContact).filter(
        RfqContact.contact_type == "email",
        RfqContact.status.in_(["sent", "opened"]),
        # "Needs follow-up" = LAST outbound contact was more than follow_up_days ago.
        # status_updated_at is stamped on the original RFQ send AND on every follow-up, so a
        # just-sent follow-up drops off the queue (no re-spam) until the window elapses again;
        # created_at is the fallback for legacy rows with no status_updated_at.
        sqlfunc.coalesce(RfqContact.status_updated_at, RfqContact.created_at) < threshold,
    )
    if getattr(user, "role", None) in (UserRole.SALES, UserRole.TRADER):
        stale_q = stale_q.join(Requisition).filter(Requisition.created_by == user.id)

    stale = stale_q.order_by(RfqContact.created_at.asc()).limit(500).all()

    req_ids = {c.requisition_id for c in stale}
    req_names: dict[int, str] = {}
    if req_ids:
        for r in db.query(Requisition.id, Requisition.name).filter(Requisition.id.in_(req_ids)).all():
            req_names[r.id] = r.name

    now = datetime.now(UTC)
    follow_ups = []
    for c in stale:
        ca = c.created_at if c.created_at else now
        days_waiting = (now - ca).days
        follow_ups.append(
            {
                "contact_id": c.id,
                "requisition_id": c.requisition_id,
                "requisition_name": req_names.get(c.requisition_id, "Unknown"),
                "vendor_name": c.vendor_name,
                "vendor_email": c.vendor_contact,
                "parts": c.parts_included or [],
                "status": c.status,
                "days_waiting": days_waiting,
            }
        )

    ctx = _base_ctx(request, user, "follow-ups")
    ctx.update({"follow_ups": follow_ups, "total": len(follow_ups)})
    return ctx


@router.get("/v2/partials/follow-ups", response_class=HTMLResponse)
async def follow_ups_list_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cross-requisition follow-up queue as HTML partial."""
    from . import template_response

    ctx = _build_follow_ups_ctx(request, user, db)
    return template_response("htmx/partials/follow_ups/list.html", ctx)


async def _deliver_follow_up(
    request: Request, db: Session, contact, *, token: str | None, is_testing: bool, body: str = ""
) -> str:
    """Send ONE follow-up email to a stale contact. Returns 'sent' | 'no_email' | 'dnc'
    | 'failed'.

    Marks contact SENT in-session (the CALLER commits) on success. Shared by the single-send
    and batch paths so both honor the same DNC hard-block, the same Graph send, and honest
    success/failure — no drift between them.

    token: a pre-fetched Graph token (batch fetches once and reuses); pass None and the helper
    fetches lazily via require_fresh_token — only after the no-email/DNC checks pass, so a
    contact with no address never triggers a token fetch.
    """
    if not contact.vendor_contact:
        return "no_email"
    dnc = (
        db.query(SiteContact)
        .filter(
            sqlfunc.lower(SiteContact.email) == contact.vendor_contact.lower(),
            SiteContact.do_not_contact.is_(True),
        )
        .first()
    )
    if dnc:
        logger.warning(
            "Follow-up skipped — do-not-contact flag set for vendor '{}' ({})",
            contact.vendor_name,
            contact.vendor_contact,
        )
        return "dnc"
    if is_testing:
        contact.status = ContactStatus.SENT
        contact.status_updated_at = datetime.now(UTC)
        return "sent"
    # Fetch the token OUTSIDE the try so a genuine session-expiry (require_fresh_token →
    # HTTPException 401) propagates to the global 401→login handler instead of being
    # mislabeled as a per-contact send failure.
    if token is None:
        from ....dependencies import require_fresh_token

        token = await require_fresh_token(request, db)
    from ....utils.graph_client import GraphClient

    gc = GraphClient(token)
    follow_up_body = (
        body
        or f"Dear {contact.vendor_name},\n\nI'm following up on our previous inquiry. Please let us know if you have availability.\n\nThank you."
    )
    payload = {
        "message": {
            "subject": f"Follow-up: {contact.subject or 'RFQ'}",
            "body": {"contentType": "Text", "content": follow_up_body},
            "toRecipients": [{"emailAddress": {"address": contact.vendor_contact}}],
        },
        "saveToSentItems": "true",
    }
    try:
        result = await gc.post_json("/me/sendMail", payload)
    except Exception as exc:
        logger.warning("Follow-up email send failed for contact {}: {}", contact.id, exc)
        return "failed"
    # GraphClient returns {"error": ...} on a 4xx / exhausted-retry WITHOUT raising — a
    # discarded return would silently mark a NON-sent email "sent" (the exact lie this
    # change removes). Check it, matching services/quote_send.py.
    if isinstance(result, dict) and result.get("error"):
        logger.warning(
            "Follow-up send failed for contact {}: Graph {} — {}",
            contact.id,
            result.get("error"),
            result.get("detail"),
        )
        return "failed"
    contact.status = ContactStatus.SENT
    contact.status_updated_at = datetime.now(UTC)
    return "sent"


@router.post("/v2/partials/follow-ups/{contact_id}/send", response_class=HTMLResponse)
async def send_follow_up_htmx(
    request: Request,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a follow-up email for a stale contact.

    Returns success card.
    """
    from . import template_response

    contact = db.get(RfqContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    require_requisition_access(db, contact.requisition_id, user, owner_id=contact.user_id, label="Contact")

    form = await request.form()
    body = (form.get("body") or "").strip()
    is_testing = os.environ.get("TESTING") == "1"

    result = await _deliver_follow_up(request, db, contact, token=None, is_testing=is_testing, body=body)
    if result == "sent":
        db.commit()
    logger.info("Follow-up {} for contact {} (vendor: {}) by {}", result, contact_id, contact.vendor_name, user.email)

    if result == "dnc":
        return HTMLResponse(
            '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
            "This vendor is on the do-not-contact list — follow-up not sent.</div>"
        )

    ctx = _base_ctx(request, user, "follow-ups")
    ctx["contact_id"] = contact_id
    ctx["vendor_name"] = contact.vendor_name or "Vendor"
    # Honest result card — only claim "sent" when the email actually went out (or in test
    # mode). no_email / failed surface an honest failure card instead of a green lie.
    if result in ("no_email", "failed"):
        ctx["reason"] = "no_email" if result == "no_email" else "send_failed"
        return template_response("htmx/partials/follow_ups/send_failed.html", ctx)
    return template_response("htmx/partials/follow_ups/sent_success.html", ctx)


@router.post("/v2/partials/follow-ups/{contact_id}/ai-draft", response_class=HTMLResponse)
async def ai_draft_follow_up(
    request: Request,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Draft a contextual follow-up body and fill the compose textarea."""
    contact = db.get(RfqContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    require_requisition_access(db, contact.requisition_id, user, owner_id=contact.user_id, label="Contact")

    days_waiting = (datetime.now(UTC) - contact.created_at).days if contact.created_at else None

    from app.services.email_drafting import draft_email

    result = await draft_email(
        "follow_up",
        {
            "vendor_name": contact.vendor_name,
            "parts": contact.parts_included or [],
            "days_waiting": days_waiting,
            "subject": contact.subject,
        },
    )
    drafted = (result or {}).get("body") or ""

    escaped = drafted.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$").replace("</", "<\\/")
    return HTMLResponse(
        f'<script>document.getElementById("follow-up-body-{contact_id}").value = `{escaped}`;</script>'
        '<p class="text-xs text-green-600 mt-1">Draft ready. Review and edit before sending.</p>'
    )


@router.post("/v2/partials/follow-ups/send-batch", response_class=HTMLResponse)
async def send_batch_follow_up(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send follow-ups to all stale contacts at once."""
    # Was request.app.state.follow_up_days — a value nothing ever set, so this
    # silently used the getattr default (2) and diverged from the queue/badge.
    threshold = datetime.now(UTC) - timedelta(days=settings.follow_up_days)

    q = db.query(RfqContact).filter(
        RfqContact.contact_type == "email",
        RfqContact.status.in_(["sent", "opened"]),
        # Last outbound contact older than the window — in lockstep with the queue + badge
        # (see _build_follow_ups_ctx); keeps a just-sent contact from being re-sent.
        sqlfunc.coalesce(RfqContact.status_updated_at, RfqContact.created_at) < threshold,
    )
    # Restricted roles act only on contacts under their own requisitions; buyer/manager/admin
    # stay global. Keep this in lockstep with follow_up_badge so the badge counts what the
    # batch acts on.
    if user.role in RESTRICTED_ROLES:
        q = q.join(Requisition, RfqContact.requisition_id == Requisition.id).filter(Requisition.created_by == user.id)
    stale = q.limit(50).all()

    # Actually SEND each stale contact's follow-up (via the shared _deliver_follow_up path,
    # same DNC block + Graph send + SENT-marking as the single send), instead of the old
    # behavior that silently marked everyone RESPONDED without emailing. Fetch the Graph
    # token ONCE and reuse it across the batch.
    is_testing = os.environ.get("TESTING") == "1"
    token = None
    if stale and not is_testing:
        from ....dependencies import require_fresh_token

        token = await require_fresh_token(request, db)

    tally = {"sent": 0, "no_email": 0, "dnc": 0, "failed": 0}
    for contact in stale:
        result = await _deliver_follow_up(request, db, contact, token=token, is_testing=is_testing)
        tally[result] += 1
        if result == "sent":
            # Commit each send immediately — a Graph send is irreversible, so a later mid-loop
            # failure/cancellation must not roll back the SENT record and re-send the same
            # vendor on the next Send-All run.
            db.commit()
    # Escalate to ERROR (Sentry-visible) when a batch sends nothing but had failures — a
    # systemic outage (expired app creds, Graph down) shouldn't be visible only as one
    # user's toast.
    log = logger.error if (tally["failed"] and not tally["sent"]) else logger.info
    log(
        "Batch follow-up by {user}: sent={sent} no_email={no_email} dnc={dnc} failed={failed}",
        user=user.email,
        **tally,
    )

    # Honest summary — report what ACTUALLY happened, never a blanket "marked responded".
    skipped = tally["no_email"] + tally["dnc"]
    parts = [f"{tally['sent']} sent"]
    if skipped:
        parts.append(f"{skipped} skipped (no address / do-not-contact)")
    if tally["failed"]:
        parts.append(f"{tally['failed']} failed")
    msg = "Follow-ups: " + ", ".join(parts) + "."
    if tally["failed"] and not tally["sent"]:
        toast_type = "error"
    elif tally["failed"] or skipped:
        toast_type = "warning"
    else:
        toast_type = "success"

    # Re-render the (now shorter / empty) queue so the surrounding page survives; the honest
    # count is surfaced via an HX-Trigger toast (base.html showToast bridge).
    from . import template_response

    ctx = _build_follow_ups_ctx(request, user, db)
    resp = template_response("htmx/partials/follow_ups/list.html", ctx)
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg, "type": toast_type}})
    return resp


@router.get("/v2/partials/follow-ups/badge", response_class=HTMLResponse)
async def follow_up_badge(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return follow-up count badge for nav sidebar."""
    threshold = datetime.now(UTC) - timedelta(days=settings.follow_up_days)
    q = db.query(sqlfunc.count(RfqContact.id)).filter(
        RfqContact.contact_type == "email",
        RfqContact.status.in_(["sent", "opened"]),
        # Last outbound contact older than the window — in lockstep with the queue + batch.
        sqlfunc.coalesce(RfqContact.status_updated_at, RfqContact.created_at) < threshold,
    )
    # Same per-owner scope as send_batch_follow_up so the badge matches the batch.
    if user.role in RESTRICTED_ROLES:
        q = q.join(Requisition, RfqContact.requisition_id == Requisition.id).filter(Requisition.created_by == user.id)
    count = q.scalar() or 0
    if count > 0:
        return HTMLResponse(
            f'<span class="ml-auto px-1.5 py-0.5 text-[10px] font-bold text-white bg-amber-500 rounded-full">{count}</span>'
        )
    return HTMLResponse("")
