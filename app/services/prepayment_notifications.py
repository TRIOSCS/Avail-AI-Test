"""prepayment_notifications.py — accounting/AP notifications for prepayment requests.

Purpose: When a buyer requests a prepayment on a cut PO, and when a manager approves,
voids, or confirms it paid, notify the affected parties. Every notice is a SINGLE
delivery: an email enqueued on the approval outbox (W3.8/§5.5 — the durable path; the
drain job sends it). The old direct admin-token Graph send, the Teams Adaptive Card,
and the in-app ActivityLog fan-outs/honesty alerts were the extra delivery systems for
the same events and were deleted; failure durability now lives on the outbox row
itself (fail_count/last_error, dead-letter cap).

  - requested / approved / voided → ONE outbox email addressed to the configured group
    DLs (``accounting_group_email`` + ``ap_group_email``), with an admin user as the
    delegated Graph SENDER (the DLs are external addresses, not Avail users).
  - paid → one outbox email per Avail recipient: the buyer, the salesperson, and every
    active manager.

The REQUESTED notice is headed "PENDING APPROVAL — DO NOT PAY YET"; the APPROVED notice
"APPROVED — OK TO WIRE" (finding #13); VOIDED is the "DO NOT WIRE" stand-down. The
beneficiary is the vendor's legal name, falling back to the payee snapshot then the card
display name (finding #14). The amount is rendered to 2 decimals honoring
``Prepayment.currency`` (finding #9). The notify functions never raise — a failed notice
must not break the request/approval.

Called by: app.routers.prepayments (request create), app.routers.htmx.buy_plans (approve
           → approved, reject → voided), app.services.buyplan_workflow (teardown → voided),
           app.services.prepayment_service (mark paid → paid), via run_prepayment_notify_bg.
Depends on: app.database (SessionLocal), app.config (settings.admin_emails),
            app.services.admin_service (get_config_values),
            app.services.approvals.notifications (outbox enqueue seam),
            app.utils.async_helpers,
            app.models (Prepayment, ApprovalRequest, ActivityLog, User).
"""

from __future__ import annotations

import asyncio
import html as html_mod
from decimal import Decimal

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalSubjectType,
    UserRole,
)
from ..models import User
from ..models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from ..models.quality_plan import Prepayment
from ..services.admin_service import get_config_values
from ..services.approvals.notifications import enqueue_email, latest_request_id
from ..utils.async_helpers import hold_bg_task, safe_background_task
from ..utils.timezones import DEFAULT_DISPLAY_TZ, format_localtime

_CONFIG_KEYS = ["accounting_group_email", "ap_group_email"]

_HEADINGS = {
    "requested": "PENDING APPROVAL — DO NOT PAY YET",
    "approved": "APPROVED — OK TO WIRE",
    "paid": "PAID — WIRE CONFIRMED",
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


# The main FastAPI event loop, registered via set_main_event_loop() during the
# lifespan. Needed because sync callers (threadpool-run routes / sync services
# driving check_completion -> _complete_plan ->
# _cancel_open_prepayment_requests_for_plan) have NO running loop of their own,
# so schedule_prepayment_notify's get_running_loop() check would otherwise miss
# and silently drop the DO-NOT-WIRE stand-down notification.
_main_event_loop: asyncio.AbstractEventLoop | None = None


def set_main_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Register the main event loop for schedule_prepayment_notify's cross-thread
    fallback.

    Called by: app.main lifespan, immediately before dispatching
    run_deferred_startup_backfills via asyncio.to_thread.
    """
    global _main_event_loop
    _main_event_loop = loop


def schedule_prepayment_notify(coro) -> None:
    """Loop-aware fire-and-forget for a prepayment-notify coroutine from a SYNC caller.

    ``run_prepayment_notify_bg(...)`` returns a coroutine; a sync service (mark-paid,
    teardown void) cannot ``await`` it. If an event loop is running (the async request that
    drove the transition) schedule it as a fire-and-forget task on it. Otherwise — a
    sync caller on a threadpool/worker thread with no loop of its own — fall back to the
    registered main event loop (see ``set_main_event_loop``) via
    ``asyncio.run_coroutine_threadsafe``, so the notification still runs as a task on the
    main loop instead of being silently dropped. If neither a running loop nor a
    registered main loop is available (bare sync/CLI/test caller, or a boot that never
    reached the lifespan's registration point), close the coroutine cleanly so nothing
    dangles and no dispatch is attempted.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        main_loop = _main_event_loop
        if main_loop is not None and main_loop.is_running():
            _dispatch_to_main_loop(coro, main_loop)
        else:
            coro.close()
    else:
        hold_bg_task(loop.create_task(coro))


def _dispatch_to_main_loop(coro, loop: asyncio.AbstractEventLoop) -> None:
    """Run *coro* as a task on *loop* from a different (non-loop) thread.

    ``asyncio.run_coroutine_threadsafe`` only hands back a ``concurrent.futures.Future``
    — not the underlying ``asyncio.Task`` — so ``hold_bg_task``'s strong-ref retention
    can't be applied from the calling (worker) thread directly. *coro* is wrapped so the
    retention, and error isolation equivalent to ``safe_background_task``, both happen on
    the loop's own thread once the wrapped coroutine actually starts running there.
    """

    async def _wrapped():
        task = asyncio.current_task()
        if task is not None:
            hold_bg_task(task)
        try:
            await coro
        except Exception:
            logger.exception("Cross-thread prepayment notification failed")

    asyncio.run_coroutine_threadsafe(_wrapped(), loop)


# ── Public notify functions ──────────────────────────────────────────


async def notify_prepayment_requested(prepayment_id: int, db: Session | None = None) -> dict:
    """Enqueue the REQUESTED (DO NOT PAY YET) outbox email to accounting/AP."""
    return await _notify(prepayment_id, "requested", db)


async def notify_prepayment_approved(prepayment_id: int, db: Session | None = None) -> dict:
    """Enqueue the APPROVED (OK TO WIRE) outbox email to accounting/AP."""
    return await _notify(prepayment_id, "approved", db)


async def notify_prepayment_voided(prepayment_id: int, db: Session | None = None, reason: str | None = None) -> dict:
    """Stand-down: tell accounting/AP a previously-authorized prepayment is VOID — DO NOT WIRE.

    One outbox email to the accounting/AP DLs. *reason* overrides the prepayment's
    persisted ``void_reason`` (the background runner passes none, so the persisted
    reason wins there). Best-effort, never raises.
    """
    return await _notify(prepayment_id, "voided", db, reason=reason)


async def notify_prepayment_paid(prepayment_id: int, db: Session | None = None) -> dict:
    """Fan out the PAID notice: one outbox email per Avail recipient.

    Recipients are deduped: the buyer (``created_by_id``), the salesperson
    (``buy_plan.submitted_by_id``, falling back to the requisition creator), and every
    active Manager-role user. Best-effort, never raises.
    """
    own_session = db is None
    if own_session:
        from ..database import SessionLocal

        db = SessionLocal()
    try:
        return _notify_paid_inner(db, prepayment_id)
    finally:
        if own_session:
            db.close()


async def _notify(prepayment_id: int, event: str, db: Session | None, reason: str | None = None) -> dict:
    """Enqueue the *event* outbox email; open + close an own session only if none was
    passed."""
    own_session = db is None
    if own_session:
        from ..database import SessionLocal

        db = SessionLocal()
    try:
        return _notify_inner(db, prepayment_id, event, reason=reason)
    finally:
        if own_session:
            db.close()


def _notify_inner(db: Session, prepayment_id: int, event: str, reason: str | None = None) -> dict:
    """Enqueue ONE outbox email to the accounting/AP DLs for *event* (single
    delivery)."""
    result: dict = {"enqueued": False, "recipients": []}
    prepayment = db.get(Prepayment, prepayment_id)
    if prepayment is None:
        logger.warning("notify_prepayment_{}: prepayment {} not found", event, prepayment_id)
        return result

    cfg = get_config_values(db, _CONFIG_KEYS)
    recipients = [a for a in ((cfg.get("accounting_group_email"), cfg.get("ap_group_email"))) if a]
    result["recipients"] = recipients
    if not recipients:
        logger.info("Prepayment {}: no accounting/AP group email configured — nothing to enqueue", prepayment_id)
        return result

    request_id = latest_request_id(db, ApprovalSubjectType.PREPAYMENT, prepayment_id)
    if request_id is None:
        logger.warning("Prepayment {} has no ApprovalRequest — {} email not enqueued", prepayment_id, event)
        return result

    sender = _admin_sender(db)
    if sender is None:
        logger.warning("Prepayment {}: no admin user to send as — {} email not enqueued", prepayment_id, event)
        return result

    # The void reason: an explicit argument wins, else the persisted column.
    effective_reason = reason or prepayment.void_reason
    approver_name, decided_at = (None, None)
    if event == "approved":
        approver_name, decided_at = _resolve_approval(db, prepayment_id)

    subject = _subject(prepayment, event, reason=effective_reason)
    html_body = _email_html(prepayment, event, approver_name, decided_at, reason=effective_reason)
    try:
        enqueue_email(
            db,
            request_id=request_id,
            recipient_user_id=sender.id,
            subject=subject,
            html=html_body,
            to=recipients,
        )
        db.commit()
        result["enqueued"] = True
    except Exception:
        db.rollback()
        logger.exception("Prepayment {} — failed to enqueue {} outbox email", prepayment_id, event)
    return result


def _admin_sender(db: Session) -> User | None:
    """The admin user who acts as the delegated Graph sender for the group-DL email.

    Prefers an admin with a live access token (same borrow as the old direct send);
    falls back to the first configured admin — the outbox dispatcher re-checks the
    token at send time. None when no ``settings.admin_emails`` user exists.
    """
    admin_users = db.query(User).filter(User.email.in_(settings.admin_emails)).all()
    if not admin_users:
        return None
    return next((a for a in admin_users if a.access_token), admin_users[0])


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
    legal: str | None = getattr(vc, "legal_name", None) if vc is not None else None
    if legal:
        return legal
    if prepayment.vendor_name:
        return prepayment.vendor_name
    if vc is not None and vc.display_name:
        display: str = vc.display_name  # legacy relationship read is untyped
        return display
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
    return format_localtime(dt, "%Y-%m-%d %H:%M %Z", tz=DEFAULT_DISPLAY_TZ)


def _confirm_url(prepayment: Prepayment) -> str | None:
    """The public tokenized "confirm wire sent" URL, or None when no live pay_token.

    Only an approved prepayment carries a ``pay_token`` (minted on approve, cleared on
    paid/void), so this resolves to a link only while a wire is genuinely pending. Base is
    ``settings.app_url`` (must point at the reachable deployment for the link to work).
    """
    if not prepayment.pay_token:
        return None
    base = (settings.app_url or "").rstrip("/")
    return f"{base}/p/confirm/{prepayment.pay_token}"


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
    if event == "voided" and prepayment.void_reason:
        facts.append(("Void reason", prepayment.void_reason))
    if event == "paid":
        if prepayment.wire_reference:
            facts.append(("Wire reference", prepayment.wire_reference))
        if prepayment.paid_by_label:
            facts.append(("Paid by", prepayment.paid_by_label))
    return facts


def _heading(event: str, reason: str | None = None) -> str:
    if event == "voided":
        return f"DO NOT WIRE — prepayment voided: {reason or '—'}"
    return _HEADINGS.get(event, _HEADINGS["requested"])


def _subject(prepayment: Prepayment, event: str, reason: str | None = None) -> str:
    return (
        f"[AVAIL] Prepayment {_heading(event, reason)} — Plan #{prepayment.buy_plan_id} ({_format_amount(prepayment)})"
    )


# Email banner treatment per event: green = money OK, red = stand-down, amber = pending.
_BANNER = {"approved": "#16a34a", "paid": "#16a34a", "voided": "#dc2626"}


def _email_html(prepayment: Prepayment, event: str, approver=None, decided_at=None, reason=None) -> str:
    """Standard AVAIL email wrapper around the prepayment facts table."""
    banner = _BANNER.get(event, "#d97706")
    rows = "".join(
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-weight:bold">{html_mod.escape(str(label))}</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(value))}</td></tr>'
        for label, value in _facts(prepayment, event, approver, decided_at)
    )
    _NOTES = {
        "approved": "This prepayment is <strong>APPROVED — OK TO WIRE</strong> the beneficiary below.",
        "paid": "This prepayment has been marked <strong>PAID — WIRE CONFIRMED</strong>.",
        "voided": "This prepayment was <strong>VOIDED — DO NOT WIRE</strong>. Claw back if already sent.",
        "requested": "This prepayment is <strong>PENDING APPROVAL — DO NOT PAY YET</strong>.",
    }
    note = _NOTES.get(event, _NOTES["requested"])
    # On the APPROVED notice, embed the public tokenized "confirm wire sent" button so the
    # (non-Avail) accounting team can mark the prepayment paid straight from the email.
    button = ""
    if event == "approved":
        confirm_url = _confirm_url(prepayment)
        if confirm_url:
            button = (
                f'<p style="margin:20px 0"><a href="{html_mod.escape(confirm_url)}" '
                f'style="display:inline-block;background:#16a34a;color:#ffffff;padding:12px 20px;'
                f'border-radius:6px;text-decoration:none;font-weight:bold">Confirm wire sent</a></p>'
                f'<p style="color:#6b7280;font-size:12px">Once the wire has gone out, click the '
                f"button above to confirm payment. This link is single-use.</p>"
            )
    return (
        f'<div style="font-family:Arial,sans-serif;max-width:640px">'
        f'<h2 style="color:{banner}">{html_mod.escape(_heading(event, reason))}</h2>'
        f"<p>{note}</p>"
        f'<table style="border-collapse:collapse;margin:16px 0">{rows}</table>'
        f"{button}"
        f'<p style="color:#6b7280;font-size:12px;margin-top:20px">'
        f"Automated prepayment notice from AVAIL.</p></div>"
    )


# ── Paid fan-out (one outbox email per Avail recipient) ──────────────


def _notify_paid_inner(db: Session, prepayment_id: int) -> dict:
    """Enqueue one PAID outbox email per fan-out recipient (single delivery).

    Recipients (deduped): the buyer (``created_by_id``), the salesperson
    (``buy_plan.submitted_by_id``, falling back to the requisition creator), and every
    active Manager-role user. Best-effort: a failure here is logged, never raised.
    """
    result: dict = {"alerted": []}
    prepayment = db.get(Prepayment, prepayment_id)
    if prepayment is None:
        logger.warning("notify_prepayment_paid: prepayment {} not found", prepayment_id)
        return result

    plan = prepayment.buy_plan
    user_ids: set[int] = set()
    if prepayment.created_by_id:
        user_ids.add(prepayment.created_by_id)
    if plan is not None:
        if plan.submitted_by_id:
            user_ids.add(plan.submitted_by_id)
        elif plan.requisition is not None and plan.requisition.created_by:
            user_ids.add(plan.requisition.created_by)
    managers = db.query(User.id).filter(User.role == UserRole.MANAGER, User.is_active.is_(True)).all()
    user_ids.update(row.id for row in managers)
    if not user_ids:
        logger.warning("Prepayment #{} paid but no buyer/salesperson/manager to alert", prepayment_id)
        return result

    request_id = latest_request_id(db, ApprovalSubjectType.PREPAYMENT, prepayment_id)
    if request_id is None:
        logger.warning("Prepayment {} has no ApprovalRequest — paid email not enqueued", prepayment_id)
        return result

    subject = _subject(prepayment, "paid")
    html_body = _email_html(prepayment, "paid")
    try:
        for uid in sorted(user_ids):
            enqueue_email(
                db,
                request_id=request_id,
                recipient_user_id=uid,
                subject=subject,
                html=html_body,
            )
        db.commit()
        result["alerted"] = sorted(user_ids)
        logger.info("Prepayment #{} paid — enqueued {} outbox email(s)", prepayment_id, len(user_ids))
    except Exception:
        db.rollback()
        logger.exception("Failed to enqueue prepayment-paid outbox emails for #{}", prepayment_id)
    return result
