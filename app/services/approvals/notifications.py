"""notifications.py — NotificationService helpers for the approval engine.

Purpose: Low-level send primitives invoked by the outbox dispatcher.
         Sends email via the Graph API (same pattern as buyplan_notifications)
         and writes in-app Notification rows.

Called by: app/jobs/approval_outbox.dispatch_pending
Depends on: app.models.notification, app.models.auth (User),
            app.utils.graph_client, app.utils.token_manager
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

# ── Lazy imports (Graph client) are done inside functions to match the pattern
# used by buyplan_notifications and avoid import-time side effects.


# Make them patchable at this module level (mirrors buyplan_notifications pattern).
try:
    from app.utils.graph_client import GraphClient  # noqa: F401
    from app.utils.token_manager import get_valid_token  # noqa: F401
except Exception:  # pragma: no cover
    logger.warning(
        "approvals.notifications: Graph imports unavailable — email dispatch disabled",
        exc_info=True,
    )
    GraphClient = None
    get_valid_token = None


async def send_email(user, subject: str, html_body: str, db: Session) -> None:
    """Send a single approval notification email via Graph API.

    Silently skips when token fetch returns nothing (user not authenticated). On any
    other error, re-raises so the dispatcher can record it.
    """
    token = await get_valid_token(user, db)
    if not token:
        logger.warning("approval email skipped — no token for {}", user.email)
        return
    gc = GraphClient(token)
    await gc.post_json(
        "/me/sendMail",
        {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": user.email}}],
            },
            "saveToSentItems": "false",
        },
    )
    logger.info("approval email sent to {}", user.email)


def write_in_app(
    db: Session,
    user_id: int,
    event_type: str,
    title: str,
    body: str | None = None,
) -> None:
    """Write one in-app Notification row for an approval event."""
    from app.models.notification import Notification

    notif = Notification(
        user_id=user_id,
        event_type=event_type,
        title=title,
        body=body,
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(notif)


def _build_email_html(payload: dict) -> tuple[str, str]:
    """Build subject + HTML body from an outbox payload dict."""
    decision = payload.get("decision", "decided")
    subject = f"Approval request {decision}"
    html = (
        f"<p>Your approval request has been <strong>{decision}</strong>.</p>"
        f'<p style="color:#6b7280;font-size:12px">This is an automated alert from AVAIL.</p>'
    )
    return subject, html


def _build_in_app(payload: dict) -> tuple[str, str, str | None]:
    """Return (event_type, title, body) for a Notification row from an outbox
    payload."""
    decision = payload.get("decision", "decided")
    event_type = f"approval_{decision}"
    title = f"Approval {decision}"
    body = payload.get("comment") or None
    return event_type, title, body
