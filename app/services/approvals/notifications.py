"""notifications.py — NotificationService helpers for the approval engine.

Purpose: Low-level send primitives invoked by the outbox dispatcher.
         Sends email via the Graph API (same pattern as buyplan_notifications)
         and writes in-app Notification rows (via the shared
         app.services.in_app_notifications seam, re-exported here).

Called by: app/jobs/approval_outbox.dispatch_pending
Depends on: app.services.in_app_notifications, app.models.auth (User),
            app.utils.graph_client, app.utils.token_manager
"""

from loguru import logger
from sqlalchemy.orm import Session

# write_in_app moved to the shared app.services.in_app_notifications seam
# (nightly-status alerting needed it outside the approvals engine). Re-exported
# here so existing imports keep working: routers/htmx/buy_plans.py imports it
# from this module and jobs/approval_outbox.py (and its tests) patch it as an
# attribute of this module (_ns.write_in_app).
from app.services.in_app_notifications import write_in_app  # noqa: F401

# ── Lazy imports (Graph client) are done inside functions to match the pattern
# used by buyplan_notifications and avoid import-time side effects.


# Make them patchable at this module level (mirrors buyplan_notifications pattern).
try:
    from app.utils.graph_client import GraphClient, build_sendmail_payload
    from app.utils.token_manager import get_valid_token
except Exception:  # pragma: no cover
    logger.warning(
        "approvals.notifications: Graph imports unavailable — email dispatch disabled",
        exc_info=True,
    )
    GraphClient = None
    get_valid_token = None
    build_sendmail_payload = None


async def send_email(user, subject: str, html_body: str, db: Session) -> None:
    """Send a single approval notification email via Graph API.

    Raises when token fetch returns nothing (user not authenticated) — like any other
    send error — so the outbox dispatcher records a failure (fail_count/last_error →
    retry, then dead-letter) instead of stamping ``sent_at`` on an email that never
    went out. Previously it returned silently and the dispatcher marked the row sent,
    losing the requester's only notice of the decision (QC 2026-08-13).
    """
    token = await get_valid_token(user, db)
    if not token:
        raise RuntimeError(f"approval email not sent — no Graph token for {user.email}")
    gc = GraphClient(token)
    await gc.post_json(
        "/me/sendMail",
        build_sendmail_payload(subject, html_body, user.email, save_to_sent="false"),
    )
    logger.info("approval email sent to {}", user.email)


def _build_email_html(payload: dict) -> tuple[str, str]:
    """Build subject + HTML body from an outbox payload dict.

    QC 2026-08-10 P1-4: tell the requester WHAT was decided, the amount, the
    reason (the rejection reason was previously discarded — the acute case is a
    prepayment requester whose only notice this is), and a link back to the
    workspace. All interpolated values are HTML-escaped.
    """
    import html as _html

    from ...config import settings

    decision = str(payload.get("decision", "decided"))
    gate = str(payload.get("gate_type") or "approval").replace("_", " ").title()
    req_id = payload.get("request_id")
    amount = payload.get("amount")
    comment = payload.get("comment")

    subject = f"[AVAIL] {gate} request {decision}" + (f" — #{req_id}" if req_id else "")
    color = "#16a34a" if decision == "approved" else "#dc2626"
    parts = [
        f"<p>Your <strong>{_html.escape(gate)}</strong> request "
        f'has been <strong style="color:{color}">{_html.escape(decision.upper())}</strong>.</p>'
    ]
    if amount:
        parts.append(f"<p>Amount: <strong>${_html.escape(str(amount))}</strong></p>")
    if comment:
        label = "Reason" if decision == "rejected" else "Note"
        parts.append(f"<p>{label}: {_html.escape(str(comment))}</p>")
    base = (settings.app_url or "").rstrip("/")
    parts.append(f'<p><a href="{base}/v2/approvals">Open in AVAIL</a></p>')
    parts.append('<p style="color:#6b7280;font-size:12px">Automated alert from AVAIL.</p>')
    return subject, "".join(parts)


def _build_in_app(payload: dict) -> tuple[str, str, str | None]:
    """Return (event_type, title, body) for a Notification row from an outbox
    payload."""
    decision = payload.get("decision", "decided")
    event_type = f"approval_{decision}"
    title = f"Approval {decision}"
    body = payload.get("comment") or None
    return event_type, title, body
