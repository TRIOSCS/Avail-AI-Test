"""prepayment_notifications.py — accounting/AP notifications for prepayment requests.

Purpose: When a buyer requests a prepayment on a cut PO, and when a manager approves it,
notify the (non-Avail) accounting + AP groups so the wire can be prepared / executed. Two
best-effort, fire-and-forget channels:
  - Email to the configured group DLs (``accounting_group_email`` + ``ap_group_email``)
    sent via a logged-in admin's DELEGATED Microsoft Graph token — there is NO app-token
    mail path, so we borrow an admin who has a live token (copied from
    buyplan_notifications.notify_stock_sale_approved).
  - A Teams Adaptive Card posted to the prepayment channel webhook
    (``prepayment_teams_webhook``).
The REQUESTED notice is headed "PENDING APPROVAL — DO NOT PAY YET"; the APPROVED notice
"APPROVED — OK TO WIRE" (finding #13). The beneficiary is the vendor's legal name, falling
back to the payee snapshot then the card display name (finding #14). The amount is rendered
to 2 decimals honoring ``Prepayment.currency`` (finding #9).

Notification honesty (finding #8): each notify returns
``{email_sent, teams_sent, recipients}``; if BOTH channels fail/skip while a group address
WAS configured (a real send was expected but nothing got out), a durable in-app ActivityLog
alert is written to the requester + admins so nobody assumes AP was told. The notify
functions never raise — a failed notice must not break the request/approval.

Called by: app.routers.prepayments (request create), app.routers.htmx.buy_plans (approve),
           via run_prepayment_notify_bg.
Depends on: app.database (SessionLocal), app.config (settings.admin_emails),
            app.services.admin_service (get_config_values),
            app.services.teams_notifications (post_teams_channel_card),
            app.utils.graph_client, app.utils.token_manager, app.utils.async_helpers,
            app.models (Prepayment, ApprovalRequest, ActivityLog, User).
"""

from __future__ import annotations

import html as html_mod
from decimal import Decimal

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import (
    ActivityType,
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalSubjectType,
    UserRole,
)
from ..models import ActivityLog, User
from ..models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from ..models.quality_plan import Prepayment
from ..services.admin_service import get_config_values
from ..services.teams_notifications import post_teams_channel_card
from ..utils.async_helpers import safe_background_task

_CONFIG_KEYS = ["accounting_group_email", "ap_group_email", "prepayment_teams_webhook"]

_HEADINGS = {
    "requested": "PENDING APPROVAL — DO NOT PAY YET",
    "approved": "APPROVED — OK TO WIRE",
}


# ── Background runner ────────────────────────────────────────────────


async def run_prepayment_notify_bg(coro_fn, prepayment_id: int) -> None:
    """Fire-and-forget a prepayment notification coroutine with its own DB session.

    Mirrors buyplan_notifications.run_v3_notify_bg's error isolation but is keyed on
    Prepayment: opens a fresh SessionLocal, verifies the Prepayment still exists (skips if
    it vanished), runs ``coro_fn(prepayment_id, db=...)``, and always closes the session.
    Exceptions are logged, never propagated. Suppressed under TESTING so the suite never
    schedules a stray background task (production behaviour is unchanged).
    """

    async def _run():
        from ..database import SessionLocal

        bg_db = SessionLocal()
        try:
            if bg_db.get(Prepayment, prepayment_id) is None:
                logger.warning("Prepayment {} vanished before notify — skipping", prepayment_id)
                return
            await coro_fn(prepayment_id, db=bg_db)
        except Exception:
            logger.exception(
                "Background {} failed for prepayment {}",
                getattr(coro_fn, "__name__", "notify"),
                prepayment_id,
            )
        finally:
            bg_db.close()

    await safe_background_task(_run(), task_name="prepayment_notification", suppress_in_testing=True)


# ── Public notify functions ──────────────────────────────────────────


async def notify_prepayment_requested(prepayment_id: int, db: Session | None = None) -> dict:
    """Notify accounting/AP that a prepayment was REQUESTED (DO NOT PAY YET)."""
    return await _notify(prepayment_id, "requested", db)


async def notify_prepayment_approved(prepayment_id: int, db: Session | None = None) -> dict:
    """Notify accounting/AP that a prepayment was APPROVED (OK TO WIRE)."""
    return await _notify(prepayment_id, "approved", db)


async def _notify(prepayment_id: int, event: str, db: Session | None) -> dict:
    """Run both channels for *event*; open + close an own session only if none was
    passed."""
    own_session = db is None
    if own_session:
        from ..database import SessionLocal

        db = SessionLocal()
    try:
        return await _notify_inner(db, prepayment_id, event)
    finally:
        if own_session:
            db.close()


async def _notify_inner(db: Session, prepayment_id: int, event: str) -> dict:
    result = {"email_sent": False, "teams_sent": False, "recipients": []}
    prepayment = db.get(Prepayment, prepayment_id)
    if prepayment is None:
        logger.warning("notify_prepayment_{}: prepayment {} not found", event, prepayment_id)
        return result

    cfg = get_config_values(db, _CONFIG_KEYS)
    recipients = [a for a in ((cfg.get("accounting_group_email"), cfg.get("ap_group_email"))) if a]
    webhook = (cfg.get("prepayment_teams_webhook") or "").strip() or None
    result["recipients"] = recipients

    approver_name, decided_at = (None, None)
    if event == "approved":
        approver_name, decided_at = _resolve_approval(db, prepayment_id)

    # ── Email channel (best-effort, isolated) ──
    if recipients:
        subject = _subject(prepayment, event)
        html_body = _email_html(prepayment, event, approver_name, decided_at)
        try:
            result["email_sent"] = bool(await _send_group_email(db, recipients, subject, html_body))
        except Exception as e:
            logger.error("Prepayment {} email channel failed: {}", prepayment_id, e)

    # ── Teams channel (best-effort, isolated) ──
    if webhook:
        card = _card(prepayment, event, approver=approver_name, decided_at=decided_at)
        try:
            await post_teams_channel_card(card, webhook)
            result["teams_sent"] = True
        except Exception as e:
            logger.error("Prepayment {} Teams channel failed: {}", prepayment_id, e)

    # ── Notification honesty (finding #8) ──
    # A real send was expected (a group address WAS configured) but nothing got out on
    # EITHER channel → write a durable in-app alert so nobody assumes AP was told.
    if recipients and not result["email_sent"] and not result["teams_sent"]:
        _write_failure_alert(db, prepayment)

    return result


# ── Approval resolution (approver + timestamp for the APPROVED notice) ──


def _resolve_approval(db: Session, prepayment_id: int) -> tuple[str | None, object | None]:
    """The approver name + decision time for this prepayment's approval request."""
    ar = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.subject_type == ApprovalSubjectType.PREPAYMENT,
            ApprovalRequest.subject_id == prepayment_id,
            ApprovalRequest.gate_type == ApprovalGateType.PREPAYMENT,
        )
        .order_by(ApprovalRequest.id.desc())
        .first()
    )
    if ar is None:
        return None, None
    recip = (
        db.query(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .filter(
            ApprovalStep.request_id == ar.id,
            ApprovalStepRecipient.status == ApprovalRecipientStatus.APPROVED,
        )
        .order_by(ApprovalStepRecipient.decided_at.desc())
        .first()
    )
    approver = None
    if recip is not None and recip.user is not None:
        approver = recip.user.name or recip.user.email
    return approver, ar.resolved_at


# ── Field helpers ────────────────────────────────────────────────────


def _beneficiary(prepayment: Prepayment) -> str:
    """Banks need the legal name (finding #14): legal_name → snapshot → display_name."""
    vc = prepayment.vendor_card
    legal = getattr(vc, "legal_name", None) if vc is not None else None
    if legal:
        return legal
    if prepayment.vendor_name:
        return prepayment.vendor_name
    if vc is not None and vc.display_name:
        return vc.display_name
    return "—"


def _format_amount(prepayment: Prepayment) -> str:
    """Amount to 2 decimals honoring the prepayment currency, e.g. USD 20,002.38
    (finding #9)."""
    amount = prepayment.total_incl_fees if prepayment.total_incl_fees is not None else Decimal("0")
    return f"{prepayment.currency or 'USD'} {amount:,.2f}"


def _po_number(prepayment: Prepayment) -> str:
    line = prepayment.buy_plan_line
    return (line.po_number if line is not None and line.po_number else None) or "—"


def _so_number(prepayment: Prepayment) -> str:
    plan = prepayment.buy_plan
    return (plan.sales_order_number if plan is not None and plan.sales_order_number else None) or "—"


def _requester(prepayment: Prepayment) -> str:
    u = prepayment.created_by
    return (u.name or u.email) if u is not None else "—"


def _fmt_dt(dt) -> str:
    try:
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(dt)


def _facts(prepayment: Prepayment, event: str, approver=None, decided_at=None) -> list[tuple[str, str]]:
    """Ordered (label, value) pairs shared by the Teams card FactSet + the email
    table."""
    facts = [
        ("Beneficiary", _beneficiary(prepayment)),
        ("Amount (incl. fees)", _format_amount(prepayment)),
        ("Payment method", prepayment.payment_method or "—"),
        ("PO #", _po_number(prepayment)),
        ("Buy Plan #", str(prepayment.buy_plan_id or "—")),
        ("SO #", _so_number(prepayment)),
        ("Test report sent", "Yes" if prepayment.test_report_sent else "No"),
        ("Requested by", _requester(prepayment)),
    ]
    if prepayment.buyer_remarks:
        facts.append(("Buyer remarks", prepayment.buyer_remarks))
    if event == "approved":
        if approver:
            facts.append(("Approved by", approver))
        if decided_at:
            facts.append(("Approved at", _fmt_dt(decided_at)))
    return facts


def _heading(event: str) -> str:
    return _HEADINGS.get(event, _HEADINGS["requested"])


def _subject(prepayment: Prepayment, event: str) -> str:
    return f"[AVAIL] Prepayment {_heading(event)} — Plan #{prepayment.buy_plan_id} ({_format_amount(prepayment)})"


def _card(prepayment: Prepayment, event: str, *, approver=None, decided_at=None) -> dict:
    """Adaptive Card: colored heading + subtitle + a FactSet of the wire facts."""
    approved = event == "approved"
    facts = [{"title": label, "value": str(value)} for label, value in _facts(prepayment, event, approver, decided_at)]
    subtitle = (
        "This prepayment is APPROVED — OK to wire the beneficiary below."
        if approved
        else "A prepayment has been requested and is awaiting manager approval. Do NOT pay yet."
    )
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": _heading(event),
                "weight": "Bolder",
                "size": "Large",
                "color": "Good" if approved else "Warning",
                "wrap": True,
            },
            {"type": "TextBlock", "text": subtitle, "isSubtle": True, "wrap": True},
            {"type": "FactSet", "facts": facts},
        ],
    }


def _email_html(prepayment: Prepayment, event: str, approver=None, decided_at=None) -> str:
    """Standard AVAIL email wrapper around the prepayment facts table."""
    approved = event == "approved"
    banner = "#16a34a" if approved else "#d97706"
    rows = "".join(
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-weight:bold">{html_mod.escape(str(label))}</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(value))}</td></tr>'
        for label, value in _facts(prepayment, event, approver, decided_at)
    )
    note = (
        "This prepayment is <strong>APPROVED — OK TO WIRE</strong> the beneficiary below."
        if approved
        else "This prepayment is <strong>PENDING APPROVAL — DO NOT PAY YET</strong>."
    )
    return (
        f'<div style="font-family:Arial,sans-serif;max-width:640px">'
        f'<h2 style="color:{banner}">{html_mod.escape(_heading(event))}</h2>'
        f"<p>{note}</p>"
        f'<table style="border-collapse:collapse;margin:16px 0">{rows}</table>'
        f'<p style="color:#6b7280;font-size:12px;margin-top:20px">'
        f"Automated prepayment notice from AVAIL.</p></div>"
    )


# ── Channels ─────────────────────────────────────────────────────────


async def _send_group_email(db: Session, to: list[str], subject: str, html: str) -> bool:
    """Send *html* to each address in *to* using a logged-in admin's delegated Graph
    token.

    Copies the delegated-admin send from
    buyplan_notifications.notify_stock_sale_approved: there is NO app-token sendMail
    path, so we borrow an admin who has a live token. If no admin has one, log + skip
    and return False (the caller records the honest failure). Returns True if at least
    one message was accepted.
    """
    from ..utils.graph_client import GraphClient
    from ..utils.token_manager import get_valid_token

    recipients = [a for a in to if a]
    if not recipients:
        return False

    admin_users = db.query(User).filter(User.email.in_(settings.admin_emails)).all()
    sender = next((a for a in admin_users if a.access_token), None)
    if sender is None:
        logger.warning("Prepayment email: no admin with a live Graph token — skipping send to {}", recipients)
        return False
    token = await get_valid_token(sender, db)
    if not token:
        logger.warning("Prepayment email: admin Graph token unavailable — skipping send to {}", recipients)
        return False

    gc = GraphClient(token)
    sent_any = False
    for addr in recipients:
        try:
            await gc.post_json(
                "/me/sendMail",
                {
                    "message": {
                        "subject": subject,
                        "body": {"contentType": "HTML", "content": html},
                        "toRecipients": [{"emailAddress": {"address": addr}}],
                    },
                    "saveToSentItems": "false",
                },
            )
            sent_any = True
            logger.info("Prepayment notice emailed to {}", addr)
        except Exception as e:
            logger.error("Prepayment notice email to {} failed: {}", addr, e)
    return sent_any


def _write_failure_alert(db: Session, prepayment: Prepayment) -> None:
    """Write a durable in-app ActivityLog alert when NO channel reached accounting/AP.

    Reuses buyplan_notifications' in-app-alert mechanism (a ``channel="system"``
    ActivityLog row per recipient). Addressed to the requester + all active admins so a
    human follows up manually. Best-effort: a failure here must not surface upward.
    """
    subject = f"Prepayment #{prepayment.id} notification FAILED — notify accounting/AP manually."
    user_ids: set[int] = set()
    if prepayment.created_by_id:
        user_ids.add(prepayment.created_by_id)
    admins = db.query(User.id).filter(User.role == UserRole.ADMIN, User.is_active.is_(True)).all()
    user_ids.update(row.id for row in admins)
    if not user_ids:
        logger.warning("Prepayment #{} notice failed but no requester/admin to alert", prepayment.id)
        return
    requisition_id = prepayment.buy_plan.requisition_id if prepayment.buy_plan is not None else None
    try:
        for uid in user_ids:
            db.add(
                ActivityLog(
                    user_id=uid,
                    activity_type=ActivityType.NOTE,
                    channel="system",
                    requisition_id=requisition_id,
                    buy_plan_id=prepayment.buy_plan_id,
                    subject=subject,
                )
            )
        db.commit()
        logger.warning(
            "Prepayment #{} notification failed on all channels — wrote {} in-app alert(s)",
            prepayment.id,
            len(user_ids),
        )
    except Exception:
        db.rollback()
        logger.exception("Failed to write prepayment notification-failure alert for #{}", prepayment.id)
