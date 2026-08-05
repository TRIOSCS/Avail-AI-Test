"""notifications.py — NotificationService helpers for the approval engine.

Purpose: Low-level send primitives invoked by the outbox dispatcher, plus the
         enqueue seam (enqueue_email / latest_request_id) that event services use
         to hand a pre-rendered email to the outbox — the ONE delivery system per
         approval-lifecycle event (W3.8/§5.5). Sends email via the Graph API (same
         pattern as buyplan_notifications). The in-app Notification write seam that
         used to be re-exported here was deleted with the write-only notifications
         channel (W2.9/§5.5).

Called by: app/jobs/approval_outbox.dispatch_pending,
           app.services.buyplan_notifications, app.services.prepayment_notifications
Depends on: app.models.approvals (ApprovalOutbox, ApprovalRequest),
            app.models.auth (User), app.utils.graph_client, app.utils.token_manager
"""

import html as html_mod

from loguru import logger
from sqlalchemy.orm import Session

# ── Lazy imports (Graph client) are done inside functions to match the pattern
# used by buyplan_notifications and avoid import-time side effects.


# Make them patchable at this module level (mirrors buyplan_notifications pattern).
try:
    from app.utils.graph_client import GraphClient
    from app.utils.token_manager import get_valid_token
except Exception:  # pragma: no cover
    logger.warning(
        "approvals.notifications: Graph imports unavailable — email dispatch disabled",
        exc_info=True,
    )
    GraphClient = None
    get_valid_token = None


async def send_email(
    user,
    subject: str,
    html_body: str,
    db: Session,
    *,
    to_addresses: list[str] | None = None,
) -> None:
    """Send a single approval notification email via Graph API.

    Sends to *to_addresses* when given (external group DLs — the row's recipient user
    acts as the delegated Graph SENDER), else to the recipient user's own address.
    Silently skips when token fetch returns nothing (user not authenticated). On any
    other error, re-raises so the dispatcher can record it.
    """
    token = await get_valid_token(user, db)
    if not token:
        logger.warning("approval email skipped — no token for {}", user.email)
        return
    recipients = to_addresses or [user.email]
    gc = GraphClient(token)
    await gc.post_json(
        "/me/sendMail",
        {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": addr}} for addr in recipients],
            },
            "saveToSentItems": "false",
        },
    )
    logger.info("approval email sent to {}", ", ".join(recipients))


def _build_email_html(payload: dict) -> tuple[str, str]:
    """Build subject + HTML body from an outbox payload dict.

    Two payload shapes:
      - Pre-rendered ("subject" + "html" keys): used verbatim. This is the W3.8
        single-path seam — event-specific notices (buy-plan lifecycle, prepayment
        accounting/AP) render their rich email at enqueue time and store it here.
      - Legacy "decided" shape ({"decision": ..., "comment": ...}): the generic
        decision notice written by approvals.service.decide, with the decision
        comment (e.g. the rejection reason) appended when present.
    """
    if payload.get("subject") and payload.get("html"):
        return payload["subject"], payload["html"]
    decision = payload.get("decision", "decided")
    subject = f"Approval request {decision}"
    comment_html = ""
    comment = (payload.get("comment") or "").strip()
    if comment:
        comment_html = (
            f'<p style="background:#f3f4f6;padding:10px;border-left:3px solid #6b7280;'
            f'margin:12px 0"><strong>Comment:</strong> {html_mod.escape(comment)}</p>'
        )
    html = (
        f"<p>Your approval request has been <strong>{decision}</strong>.</p>"
        f"{comment_html}"
        f'<p style="color:#6b7280;font-size:12px">This is an automated alert from AVAIL.</p>'
    )
    return subject, html


# ── Enqueue seam (single-path delivery, W3.8/§5.5) ────────────────────────────


def latest_request_id(db: Session, subject_type: str, subject_id: int) -> int | None:
    """PK of the newest ApprovalRequest for (subject_type, subject_id), any status.

    The outbox FK anchor for an event's email. Lifecycle events all trail a submit that
    opened an engine request, so this only misses on pre-engine legacy data.
    """
    from sqlalchemy import select

    from app.models.approvals import ApprovalRequest

    return (
        db.execute(
            select(ApprovalRequest.id)
            .where(
                ApprovalRequest.subject_type == subject_type,
                ApprovalRequest.subject_id == subject_id,
            )
            .order_by(ApprovalRequest.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def enqueue_email(
    db: Session,
    *,
    request_id: int,
    recipient_user_id: int,
    subject: str,
    html: str,
    to: list[str] | None = None,
) -> None:
    """Add ONE email ApprovalOutbox row carrying a pre-rendered subject/html payload.

    ``to`` = external addresses (group DLs); the recipient user is then the delegated
    Graph SENDER, not the addressee. Does not commit — the caller owns the transaction.
    """
    from app.models.approvals import ApprovalOutbox

    payload: dict = {"subject": subject, "html": html}
    if to:
        payload["to"] = to
    db.add(
        ApprovalOutbox(
            request_id=request_id,
            recipient_user_id=recipient_user_id,
            channel="email",
            payload=payload,
        )
    )
