"""buyplan_notifications.py — Unified buy plan notification service.

Handles notifications for all buy plan state transitions:
- Submit  → email + Teams + in-app to managers
- Approve → email + Teams + in-app to buyers + salesperson
- Reject  → email + in-app to salesperson
- SO Verified → in-app to buyers
- SO Rejected/Halted → email + Teams DM + in-app to salesperson (urgent)
- PO Confirmed → in-app to ops (routine)
- PO Rejected → email + Teams DM + in-app to the line's buyer (urgent)
- Issue Flagged → in-app + Teams to manager
- Completed → in-app to salesperson (routine)
- Resubmit → email + in-app to managers
- Stock Sale Approved → email to logistics/accounting + in-app + Teams
- Cancelled → in-app + Teams to submitter (lines cascade-cancelled)
- Nudge (buyer / ops) → reminder when a line sits unconfirmed past its SLA

Called by: routers/htmx_views.py, jobs/inventory_jobs.py, buyplan_service.py
Depends on: models, config, utils/graph_client, teams_notifications
"""

import asyncio
import html as html_mod

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import ActivityType, UserRole
from ..models import ActivityLog, User
from ..models.buy_plan import BuyPlan, BuyPlanLine
from ..utils.async_helpers import safe_background_task

# ── Background runner ────────────────────────────────────────────────


async def run_v3_notify_bg(coro_factory, plan_id: int, **kwargs):
    """Fire-and-forget a notification coroutine with its own DB session."""

    async def _run():
        from ..database import SessionLocal

        bg_db = SessionLocal()
        try:
            bg_plan = bg_db.get(BuyPlan, plan_id)
            if bg_plan:
                await coro_factory(bg_plan, bg_db, **kwargs)
        except Exception:
            logger.exception("Background {} failed for buy plan {}", coro_factory.__name__, plan_id)
        finally:
            bg_db.close()

    await safe_background_task(_run(), task_name="buyplan_notification")


# Backward-compatible alias
run_notify_bg = run_v3_notify_bg


# ── Helpers ──────────────────────────────────────────────────────────


def _plan_context(plan: BuyPlan, db: Session) -> dict:
    """Extract common context fields from a buy plan."""
    from ..models import Quote

    submitter = db.get(User, plan.submitted_by_id) if plan.submitted_by_id else None
    quote = db.get(Quote, plan.quote_id) if plan.quote_id else None
    customer_name = ""
    quote_number = ""
    if quote:
        quote_number = quote.quote_number or ""
        if quote.customer_site and hasattr(quote.customer_site, "company") and quote.customer_site.company:
            customer_name = quote.customer_site.company.name
        elif quote.customer_site:
            customer_name = quote.customer_site.site_name or ""
    elif plan.requisition and plan.requisition.customer_name:
        customer_name = plan.requisition.customer_name
    elif plan.requisition and plan.requisition.customer_site and plan.requisition.customer_site.company:
        customer_name = plan.requisition.customer_site.company.name
    return {
        "submitter": submitter,
        "submitter_name": submitter.name or submitter.email if submitter else "Unknown",
        "customer_name": customer_name,
        "quote_number": quote_number,
    }


def _lines_html(plan: BuyPlan) -> tuple[str, float]:
    """Build HTML table rows for plan lines.

    Returns (rows_html, total_cost).
    """
    rows = ""
    total = 0.0
    for line in plan.lines or []:
        cost = float(line.unit_cost or 0) * (line.quantity or 0)
        total += cost
        offer = line.offer
        mpn = offer.mpn if offer else "—"
        vendor = offer.vendor_name if offer else "—"
        lead = offer.lead_time if offer else "—"
        rows += (
            f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(mpn))}</td>'
            f'<td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(vendor))}</td>'
            f'<td style="padding:6px 10px;border:1px solid #e5e7eb">{line.quantity:,}</td>'
            f'<td style="padding:6px 10px;border:1px solid #e5e7eb">${float(line.unit_cost or 0):.4f}</td>'
            f'<td style="padding:6px 10px;border:1px solid #e5e7eb">${cost:,.2f}</td>'
            f'<td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(lead or ""))}</td></tr>'
        )
    return rows, total


def _lines_table(plan: BuyPlan) -> tuple[str, float]:
    """Build the full 6-column plan-lines HTML table (MPN/Vendor/Qty/Unit/Total/Lead).

    Returns (table_html, total_cost). Shared by the submit and stock-sale emails.
    """
    rows, total = _lines_html(plan)
    table = (
        f'<table style="border-collapse:collapse;width:100%;margin:16px 0">'
        f'<thead><tr style="background:#f3f4f6">'
        f'<th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">MPN</th>'
        f'<th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Vendor</th>'
        f'<th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Qty</th>'
        f'<th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Unit Cost</th>'
        f'<th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Line Total</th>'
        f'<th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Lead</th>'
        f"</tr></thead><tbody>{rows}</tbody>"
        f'<tfoot><tr style="background:#f3f4f6;font-weight:bold">'
        f'<td colspan="4" style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Total</td>'
        f'<td style="padding:8px 10px;border:1px solid #e5e7eb">${total:,.2f}</td>'
        f'<td style="padding:8px 10px;border:1px solid #e5e7eb"></td></tr></tfoot></table>'
    )
    return table, total


def _wrap_email(title: str, body_inner: str) -> str:
    """Wrap content in the standard AVAIL email template."""
    return (
        f'<div style="font-family:Arial,sans-serif;max-width:700px">'
        f'<h2 style="color:#2563eb">{html_mod.escape(title)}</h2>'
        f"{body_inner}"
        f'<p style="color:#6b7280;font-size:12px;margin-top:20px">'
        f"This is an automated alert from AVAIL.</p></div>"
    )


async def _send_email(
    user: User,
    subject: str,
    html_body: str,
    db: Session,
    *,
    pref_attr: str | None = "notify_buyplan_email_enabled",
):
    """Send an email to a single user via Graph API.

    Honors the recipient's per-user opt-out preference named by *pref_attr*: when that
    boolean column is False the Graph send is skipped entirely (no token fetch, no client
    build). Defaults to ``notify_buyplan_email_enabled`` so every existing buy-plan caller
    keeps its gate unchanged; the re-source broadcast passes
    ``notify_resource_alert_enabled``. Pass ``pref_attr=None`` to bypass the gate. The
    caller's in-app ``ActivityLog`` row is written separately and is unaffected — only the
    email channel is suppressed.
    """
    from ..utils.graph_client import GraphClient
    from ..utils.token_manager import get_valid_token

    if pref_attr and not getattr(user, pref_attr, True):
        logger.info("email suppressed (opted out: {}) for {}", pref_attr, user.email)
        return

    try:
        token = await get_valid_token(user, db)
        if not token:
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
        logger.info("buy plan email sent to {}", user.email)
    except Exception as e:
        logger.error("Failed to send buy plan email to {}: {}", user.email, e)


# ── Reuse Teams helpers ──────────────────────────────────────────────


async def _teams_channel(message: str):
    """Post to Teams channel (delegates to shared teams_notifications module)."""
    from app.services.teams_notifications import post_teams_channel

    await post_teams_channel(message)


async def _teams_channel_card(card: dict):
    """Post a full Adaptive Card to the Teams channel (delegates to
    teams_notifications)."""
    from app.services.teams_notifications import post_teams_channel_card

    await post_teams_channel_card(card)


async def _teams_dm(user: User, message: str, db: Session):
    """Send Teams DM (delegates to shared teams_notifications module)."""
    from app.services.teams_notifications import send_teams_dm

    await send_teams_dm(user, message, db)


# ── Audit Trail ─────────────────────────────────────────────────────


def log_buyplan_activity(
    db: Session,
    user_id: int,
    plan: "BuyPlan",
    activity_type: str,
    detail: str = "",
):
    """Create an ActivityLog entry for a buy plan state change.

    Stores plan linkage via subject and notes fields, with requisition_id FK.
    """
    db.add(
        ActivityLog(
            user_id=user_id,
            activity_type=activity_type,
            channel="system",
            requisition_id=plan.requisition_id,
            buy_plan_id=plan.id,
            subject=f"Buy Plan #{plan.id}: {detail}" if detail else f"Buy Plan #{plan.id}",
            notes=f"plan_id={plan.id} status={plan.status}",
        )
    )


# ── Notification Functions ───────────────────────────────────────────


async def notify_submitted(plan: BuyPlan, db: Session):
    """Notify managers that a buy plan needs approval."""
    ctx = _plan_context(plan, db)
    table, total = _lines_table(plan)

    notes_html = ""
    if plan.salesperson_notes:
        notes_html = f'<p style="background:#f0f9ff;padding:10px;border-left:3px solid #2563eb;margin:12px 0"><strong>Sales Notes:</strong> {html_mod.escape(str(plan.salesperson_notes))}</p>'

    body = (
        f"<p><strong>{html_mod.escape(ctx['submitter_name'])}</strong> submitted a buy plan.</p>"
        f"<p>Customer: <strong>{html_mod.escape(ctx['customer_name'])}</strong> | "
        f"Quote: <strong>{html_mod.escape(ctx['quote_number'])}</strong> | "
        f"SO#: <strong>{html_mod.escape(plan.sales_order_number or '')}</strong></p>"
        f"{notes_html}"
        f"{table}"
    )
    html_body = _wrap_email("Buy Plan — Approval Required", body)

    # Email to managers/admins
    managers = db.query(User).filter(User.role.in_([UserRole.MANAGER, UserRole.ADMIN])).all()
    if not managers:
        managers = db.query(User).filter(User.email.in_(settings.admin_emails)).all()

    await asyncio.gather(
        *[_send_email(m, f"[AVAIL] Buy Plan Approval — {ctx['customer_name']}", html_body, db) for m in managers]
    )

    # In-app
    for m in managers:
        db.add(
            ActivityLog(
                user_id=m.id,
                activity_type=ActivityType.BUYPLAN_PENDING,
                channel="system",
                requisition_id=plan.requisition_id,
                buy_plan_id=plan.id,
                subject=f"Buy Plan #{plan.id} needs approval — {ctx['submitter_name']}",
            )
        )
    db.commit()

    # Teams
    await _teams_channel(
        f"**Buy Plan #{plan.id} — Approval Required**\n\n"
        f"Submitted by: {ctx['submitter_name']}\n"
        f"Customer: {ctx['customer_name']} | SO#: {plan.sales_order_number or '—'}\n"
        f"Total: ${total:,.2f} | {len(plan.lines or [])} lines"
    )


async def notify_approved(plan: BuyPlan, db: Session):
    """Notify buyers and salesperson that the plan was approved."""
    ctx = _plan_context(plan, db)
    rows, total = _lines_html(plan)

    # Collect unique buyers
    buyer_ids = {ln.buyer_id for ln in (plan.lines or []) if ln.buyer_id}
    buyers = db.query(User).filter(User.id.in_(buyer_ids)).all() if buyer_ids else []

    # Email each buyer with their assigned lines
    for buyer in buyers:
        my_lines = [ln for ln in (plan.lines or []) if ln.buyer_id == buyer.id]
        buyer_rows = ""
        for ln in my_lines:
            offer = ln.offer
            mpn = offer.mpn if offer else "—"
            vendor = offer.vendor_name if offer else "—"
            buyer_rows += (
                f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(mpn))}</td>'
                f'<td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(vendor))}</td>'
                f'<td style="padding:6px 10px;border:1px solid #e5e7eb">{ln.quantity:,}</td>'
                f'<td style="padding:6px 10px;border:1px solid #e5e7eb">${float(ln.unit_cost or 0):.4f}</td></tr>'
            )
        body = (
            f"<p>Buy plan #{plan.id} has been approved. Please create POs for your assigned lines:</p>"
            f"<p>Customer: <strong>{html_mod.escape(ctx['customer_name'])}</strong> | SO#: <strong>{html_mod.escape(plan.sales_order_number or '')}</strong></p>"
            f'<table style="border-collapse:collapse;width:100%;margin:16px 0">'
            f'<thead><tr style="background:#f3f4f6">'
            f'<th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">MPN</th>'
            f'<th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Vendor</th>'
            f'<th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Qty</th>'
            f'<th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Unit Cost</th>'
            f"</tr></thead><tbody>{buyer_rows}</tbody></table>"
        )
        html_body = _wrap_email("Buy Plan Approved — POs Required", body)
        await _send_email(buyer, f"[AVAIL] POs Required — {ctx['customer_name']}", html_body, db)
        db.add(
            ActivityLog(
                user_id=buyer.id,
                activity_type="buyplan_approved",
                channel="system",
                requisition_id=plan.requisition_id,
                buy_plan_id=plan.id,
                subject=f"Buy plan #{plan.id} approved — create POs ({len(my_lines)} lines)",
            )
        )

    # Notify salesperson
    if ctx["submitter"]:
        db.add(
            ActivityLog(
                user_id=ctx["submitter"].id,
                activity_type="buyplan_approved",
                channel="system",
                requisition_id=plan.requisition_id,
                buy_plan_id=plan.id,
                subject=f"Your buy plan #{plan.id} was approved",
            )
        )

    db.commit()

    await _teams_channel(
        f"**Buy Plan #{plan.id} — Approved**\n\n"
        f"Customer: {ctx['customer_name']} | ${total:,.2f}\n"
        f"Buyers notified: {', '.join(b.name or b.email for b in buyers)}"
    )
    await asyncio.gather(
        *[
            _teams_dm(
                b,
                f"Buy Plan #{plan.id} approved — {len([ln for ln in plan.lines if ln.buyer_id == b.id])} POs needed",
                db,
            )
            for b in buyers
        ]
    )


async def notify_rejected(plan: BuyPlan, db: Session):
    """Notify salesperson that the plan was rejected."""
    ctx = _plan_context(plan, db)
    if not ctx["submitter"]:
        return

    approver = db.get(User, plan.approved_by_id) if plan.approved_by_id else None
    approver_name = approver.name or approver.email if approver else "Manager"

    body = (
        f"<p>Your buy plan #{plan.id} was rejected by <strong>{html_mod.escape(approver_name)}</strong>.</p>"
        f"<p>Customer: <strong>{html_mod.escape(ctx['customer_name'])}</strong></p>"
    )
    if plan.approval_notes:
        body += f'<p style="background:#fef2f2;padding:10px;border-left:3px solid #dc2626;margin:12px 0"><strong>Reason:</strong> {html_mod.escape(str(plan.approval_notes))}</p>'

    html_body = _wrap_email("Buy Plan Rejected", body)
    await _send_email(ctx["submitter"], f"[AVAIL] Buy Plan Rejected — {ctx['customer_name']}", html_body, db)

    db.add(
        ActivityLog(
            user_id=ctx["submitter"].id,
            activity_type="buyplan_rejected",
            channel="system",
            requisition_id=plan.requisition_id,
            subject=f"Buy plan #{plan.id} rejected — {approver_name}",
        )
    )
    db.commit()

    await _teams_dm(
        ctx["submitter"], f"Buy Plan #{plan.id} was rejected: {plan.approval_notes or 'No reason given'}", db
    )


async def notify_so_verified(plan: BuyPlan, db: Session):
    """Notify buyers that SO has been verified — they can proceed."""
    buyer_ids = {ln.buyer_id for ln in (plan.lines or []) if ln.buyer_id}
    for bid in buyer_ids:
        db.add(
            ActivityLog(
                user_id=bid,
                activity_type="buyplan_approved",
                channel="system",
                requisition_id=plan.requisition_id,
                buy_plan_id=plan.id,
                subject=f"SO# {plan.sales_order_number} verified — proceed with POs (plan #{plan.id})",
            )
        )
    db.commit()


async def notify_so_rejected(plan: BuyPlan, db: Session, action: str):
    """Notify salesperson that SO was rejected or halted."""
    ctx = _plan_context(plan, db)
    if not ctx["submitter"]:
        return

    label = "halted" if action == "halt" else "rejected"
    body = (
        f"<p>SO verification for buy plan #{plan.id} was <strong>{label}</strong>.</p>"
        f"<p>SO#: <strong>{html_mod.escape(plan.sales_order_number or '')}</strong></p>"
    )
    if plan.so_rejection_note:
        body += f'<p style="background:#fef2f2;padding:10px;border-left:3px solid #dc2626;margin:12px 0"><strong>Reason:</strong> {html_mod.escape(str(plan.so_rejection_note))}</p>'

    html_body = _wrap_email(f"SO Verification {label.title()}", body)
    await _send_email(ctx["submitter"], f"[AVAIL] SO {label.title()} — Plan #{plan.id}", html_body, db)

    db.add(
        ActivityLog(
            user_id=ctx["submitter"].id,
            activity_type="buyplan_rejected",
            channel="system",
            requisition_id=plan.requisition_id,
            buy_plan_id=plan.id,
            subject=f"SO# {plan.sales_order_number} {label} — plan #{plan.id}",
        )
    )
    db.commit()

    # Urgent tier: SO kickback → salesperson also gets a Teams DM.
    await _teams_dm(
        ctx["submitter"],
        f"SO verification for Buy Plan #{plan.id} was {label}: {plan.so_rejection_note or 'No reason given'}",
        db,
    )


async def notify_po_confirmed(plan: BuyPlan, db: Session, line_id: int):
    """Notify ops verification group that a PO was confirmed and needs verification."""
    from ..models.buy_plan import BuyPlanLine, VerificationGroupMember

    line = db.get(BuyPlanLine, line_id)
    mpn = line.offer.mpn if line and line.offer else "—"
    po_num = line.po_number or "—"

    ops_members = db.query(VerificationGroupMember).filter(VerificationGroupMember.is_active.is_(True)).all()
    for m in ops_members:
        db.add(
            ActivityLog(
                user_id=m.user_id,
                activity_type=ActivityType.BUYPLAN_PENDING,
                channel="system",
                requisition_id=plan.requisition_id,
                buy_plan_id=plan.id,
                subject=f"PO {po_num} needs verification — {mpn} (plan #{plan.id})",
            )
        )
    db.commit()


async def notify_po_rejected(plan: BuyPlan, db: Session, line_id: int):
    """Notify the line's buyer that ops kicked back their PO (urgent tier).

    Urgent = email + Teams DM + in-app. The line is reset to AWAITING_PO with a
    ``po_rejection_note`` (set by buyplan_workflow.verify_po reject path); this tells
    the buyer to re-issue the PO. Skips silently if the line has no assigned buyer.
    """
    line = db.get(BuyPlanLine, line_id)
    if not line:
        return
    buyer = db.get(User, line.buyer_id) if line.buyer_id else None
    if not buyer:
        logger.warning("PO rejected: line {} has no buyer, skipping notify", line_id)
        return

    ctx = _plan_context(plan, db)
    offer = line.offer
    mpn = offer.mpn if offer else "—"
    reason = line.po_rejection_note or "No reason given"

    body = (
        f"<p>Your PO for buy plan #{plan.id} was <strong>kicked back</strong> by ops "
        f"and needs to be re-issued.</p>"
        f"<p>Customer: <strong>{html_mod.escape(ctx['customer_name'])}</strong> | "
        f"MPN: <strong>{html_mod.escape(str(mpn))}</strong> | "
        f"SO#: <strong>{html_mod.escape(plan.sales_order_number or '')}</strong></p>"
        f'<p style="background:#fef2f2;padding:10px;border-left:3px solid #dc2626;margin:12px 0">'
        f"<strong>Reason:</strong> {html_mod.escape(str(reason))}</p>"
    )
    html_body = _wrap_email("PO Kicked Back — Action Required", body)
    await _send_email(buyer, f"[AVAIL] PO Kicked Back — {ctx['customer_name']}", html_body, db)

    db.add(
        ActivityLog(
            user_id=buyer.id,
            activity_type="buyplan_rejected",
            channel="system",
            requisition_id=plan.requisition_id,
            buy_plan_id=plan.id,
            subject=f"PO kicked back — re-issue PO for {mpn} (plan #{plan.id}): {reason}",
        )
    )
    db.commit()

    await _teams_dm(
        buyer,
        f"PO for Buy Plan #{plan.id} ({mpn}) was kicked back — re-issue required. Reason: {reason}",
        db,
    )


async def notify_completed(plan: BuyPlan, db: Session):
    """Notify salesperson that the plan is complete."""
    ctx = _plan_context(plan, db)
    if not ctx["submitter"]:
        return

    # Routine tier: completion is in-app only (no email, no Teams).
    db.add(
        ActivityLog(
            user_id=ctx["submitter"].id,
            activity_type=ActivityType.BUYPLAN_COMPLETED,
            channel="system",
            requisition_id=plan.requisition_id,
            buy_plan_id=plan.id,
            subject=f"Buy plan #{plan.id} completed — {ctx['customer_name']}",
        )
    )
    db.commit()


async def notify_stock_sale_approved(plan: BuyPlan, db: Session):
    """Notify logistics/accounting that a stock sale was approved (no PO required).

    For stock sales that auto-complete, sends an email to the stock_sale_notify_emails
    list and creates an in-app notification for the submitter. Also posts to Teams.
    """
    from ..utils.graph_client import GraphClient
    from ..utils.token_manager import get_valid_token

    ctx = _plan_context(plan, db)
    table, total = _lines_table(plan)

    approver = db.get(User, plan.approved_by_id) if plan.approved_by_id else None
    approver_name = (approver.name or approver.email) if approver else "Manager (email token)"

    body = (
        f"<p>Approved by <strong>{html_mod.escape(str(approver_name))}</strong>.</p>"
        f"<p>This is an internal stock sale — no purchase orders are needed.</p>"
        f'<div style="background:#f3f4f6;padding:12px;border-radius:6px;margin:12px 0">'
        f'<p style="margin:0"><strong>Submitted by:</strong> {html_mod.escape(ctx["submitter_name"])}</p>'
        f'<p style="margin:4px 0 0"><strong>Acctivate SO#:</strong> {html_mod.escape(str(plan.sales_order_number or "N/A"))}</p>'
        f"</div>"
        f"{table}"
    )
    html_body = _wrap_email("Stock Sale Approved — No PO Required", body)

    # Send to stock_sale_notify_emails using an admin user's token
    admin_users = db.query(User).filter(User.email.in_(settings.admin_emails)).all()
    sender = next((a for a in admin_users if a.access_token), None)
    if sender:
        token = await get_valid_token(sender, db)
        if token:
            gc = GraphClient(token)

            async def _send_stock_email(email_addr):
                try:
                    await gc.post_json(
                        "/me/sendMail",
                        {
                            "message": {
                                "subject": f"[AVAIL] Stock Sale Approved — Plan #{plan.id}",
                                "body": {"contentType": "HTML", "content": html_body},
                                "toRecipients": [{"emailAddress": {"address": email_addr}}],
                            },
                            "saveToSentItems": "false",
                        },
                    )
                    logger.info("stock sale email sent to {}", email_addr)
                except Exception as e:
                    logger.error("Failed to send stock sale email to {}: {}", email_addr, e)

            await asyncio.gather(*[_send_stock_email(e) for e in settings.stock_sale_notify_emails])

    # In-app notification to submitter
    if ctx["submitter"]:
        db.add(
            ActivityLog(
                user_id=ctx["submitter"].id,
                activity_type=ActivityType.BUYPLAN_COMPLETED,
                channel="system",
                requisition_id=plan.requisition_id,
                buy_plan_id=plan.id,
                subject=f"Stock sale #{plan.id} approved and completed — no PO required",
            )
        )
    db.commit()

    # Teams channel post
    await _teams_channel(
        f"**Buy Plan #{plan.id} — Stock Sale Approved**\n\n"
        f"Approved by: {approver_name}\n"
        f"Submitted by: {ctx['submitter_name']}\n"
        f"Total: ${total:,.2f} | Type: Stock Sale (no PO required)"
    )


async def notify_cancelled(plan: BuyPlan, db: Session):
    """Notify the submitter (in-app + Teams DM) and the channel that a plan was
    cancelled."""
    ctx = _plan_context(plan, db)
    canceller = db.get(User, plan.cancelled_by_id) if plan.cancelled_by_id else None
    canceller_name = (canceller.name or canceller.email) if canceller else "\u2014"
    reason = plan.cancellation_reason or "No reason given"

    if ctx["submitter"]:
        db.add(
            ActivityLog(
                user_id=ctx["submitter"].id,
                activity_type=ActivityType.BUYPLAN_CANCELLED,
                channel="system",
                requisition_id=plan.requisition_id,
                buy_plan_id=plan.id,
                subject=f"Buy plan #{plan.id} cancelled by {canceller_name}: {reason}",
            )
        )
    db.commit()

    if ctx["submitter"]:
        await _teams_dm(
            ctx["submitter"],
            f"Buy Plan #{plan.id} was cancelled by {canceller_name}: {reason}",
            db,
        )
    _so_num = plan.sales_order_number or "\u2014"
    await _teams_channel(
        f"**Buy Plan #{plan.id} \u2014 Cancelled**\n\n"
        f"Cancelled by: {canceller_name}\n"
        f"Customer: {ctx['customer_name']} | SO#: {_so_num}\n"
        f"Reason: {reason}"
    )


async def notify_nudge_buyer(plan: BuyPlan, line: BuyPlanLine, db: Session):
    """Remind the assigned buyer that a line still has no confirmed PO.

    In-app + Teams DM. Does NOT commit \u2014 the nudge job stamps last_nudge_at and commits.
    Called by: jobs/inventory_jobs.py _job_buyplan_nudge.
    """
    buyer = db.get(User, line.buyer_id) if line.buyer_id else None
    if not buyer:
        logger.warning("Nudge buyer: line {} has no buyer, skipping", line.id)
        return False

    offer = line.offer
    mpn = offer.mpn if offer else "\u2014"
    vendor = offer.vendor_name if offer else "\u2014"
    ctx = _plan_context(plan, db)

    db.add(
        ActivityLog(
            user_id=buyer.id,
            activity_type=ActivityType.BUYPLAN_PENDING,
            channel="system",
            requisition_id=plan.requisition_id,
            buy_plan_id=plan.id,
            subject=f"Reminder \u2014 PO still needed for {mpn} (plan #{plan.id})",
            notes=f"nudge line_id={line.id} status=awaiting_po",
        )
    )
    _so_num_buyer = plan.sales_order_number or "\u2014"
    await _teams_dm(
        buyer,
        f"**Reminder \u2014 PO Required**\n\n"
        f"Plan #{plan.id} | {mpn} from {vendor}\n"
        f"Customer: {ctx['customer_name']} | SO#: {_so_num_buyer}\n"
        f"This line has been awaiting a PO for over {settings.buyplan_nudge_buyer_hours}h.",
        db,
    )

    return True


async def notify_nudge_ops(plan: BuyPlan, line: BuyPlanLine, db: Session):
    """Remind the ops verification group that a confirmed PO still needs verification.

    In-app to each active ops member (no Teams DM \u2014 group-level, would be spammy).
    Does NOT commit \u2014 the nudge job stamps last_nudge_at and commits.
    Called by: jobs/inventory_jobs.py _job_buyplan_nudge.
    """
    from ..models.buy_plan import VerificationGroupMember

    ops_members = db.query(VerificationGroupMember).filter(VerificationGroupMember.is_active.is_(True)).all()
    if not ops_members:
        logger.warning("Nudge ops: line {} \u2014 no active ops members, skipping", line.id)
        return False

    offer = line.offer
    mpn = offer.mpn if offer else "\u2014"
    po_num = line.po_number or "\u2014"
    for m in ops_members:
        db.add(
            ActivityLog(
                user_id=m.user_id,
                activity_type=ActivityType.BUYPLAN_PENDING,
                channel="system",
                requisition_id=plan.requisition_id,
                buy_plan_id=plan.id,
                subject=f"Reminder \u2014 PO {po_num} ({mpn}) needs verification (plan #{plan.id})",
                notes=f"nudge line_id={line.id} status=pending_verify",
            )
        )
    return True


# \u2500\u2500 Re-source (cut PO cancelled \u2192 deal needs backfill) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _resource_context(plan: BuyPlan, line: BuyPlanLine, db: Session, reason: str) -> dict:
    """Common facts for the re-source broadcast (card + email + DM + in-app)."""
    ctx = _plan_context(plan, db)
    req = plan.requisition
    offer = line.offer
    requirement = line.requirement
    mpn = (offer.mpn if offer else None) or (requirement.primary_mpn if requirement else None) or "\u2014"
    description = (requirement.description if requirement else "") or ""
    customer = ctx.get("customer_name") or (req.customer_name if req else "") or "\u2014"
    req_label = (req.name if req else "") or f"Requisition #{plan.requisition_id}"
    vendor = (offer.vendor_name if offer else None) or "\u2014"
    return {
        "mpn": mpn,
        "description": description,
        "qty": line.quantity or 0,
        "customer": customer,
        "req_label": req_label,
        "vendor": vendor,
        "reason": reason or "No reason given",
        "plan_id": plan.id,
        "deep_link": f"{settings.app_url}/v2/buy-plans/{plan.id}",
    }


def _resource_facts(rc: dict) -> list[tuple[str, str]]:
    """Ordered (label, value) pairs shared by the card FactSet and the email table."""
    part = rc["mpn"] + (f" \u2014 {rc['description']}" if rc["description"] else "")
    return [
        ("Part", part),
        ("Quantity", f"{rc['qty']:,}"),
        ("Customer", rc["customer"]),
        ("Requisition", rc["req_label"]),
        ("Canceled vendor", rc["vendor"]),
        ("Reason", rc["reason"]),
    ]


def _resource_card(rc: dict) -> dict:
    """Adaptive Card: Attention-colored header, FactSet, and a 'Claim this line' button."""
    facts = [{"title": label, "value": str(value)} for label, value in _resource_facts(rc)]
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "URGENT \u2014 Re-source needed",
                "weight": "Bolder",
                "size": "Large",
                "color": "Attention",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": f"A vendor PO was cancelled on Buy Plan #{rc['plan_id']} \u2014 backfill needed.",
                "isSubtle": True,
                "wrap": True,
            },
            {"type": "FactSet", "facts": facts},
        ],
        "actions": [{"type": "Action.OpenUrl", "title": "Claim this line", "url": rc["deep_link"]}],
    }


def _resource_email_html(rc: dict) -> str:
    """Standard AVAIL email wrapper around the re-source facts table + claim button."""
    rows = "".join(
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-weight:bold">{html_mod.escape(str(label))}</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(value))}</td></tr>'
        for label, value in _resource_facts(rc)
    )
    body = (
        '<p style="color:#b91c1c;font-weight:bold">A vendor PO was cancelled \u2014 '
        "this deal needs to be re-sourced.</p>"
        f'<table style="border-collapse:collapse;margin:16px 0">{rows}</table>'
        f'<p><a href="{html_mod.escape(rc["deep_link"])}" '
        'style="background:#dc2626;color:#fff;padding:10px 16px;border-radius:6px;'
        'text-decoration:none;display:inline-block">Claim this line</a></p>'
    )
    return _wrap_email("URGENT \u2014 Re-source needed", body)


def _resource_dm_text(rc: dict) -> str:
    """Plain-text Teams DM body for the re-source broadcast."""
    return (
        "**URGENT \u2014 Re-source needed**\n\n"
        f"{rc['mpn']} \u00d7 {rc['qty']:,} | Customer: {rc['customer']} | {rc['req_label']}\n"
        f"Canceled vendor: {rc['vendor']} \u2014 {rc['reason']}\n"
        f"Claim this line: {rc['deep_link']}"
    )


async def notify_resource_requested(
    plan: BuyPlan,
    db: Session,
    *,
    line_id: int,
    actor_id: int,
    reason: str = "",
):
    """Broadcast an URGENT re-source alert when a buyer cancels a vendor PO and re-
    sources.

    Recipients = every active buyer except the actor, plus the deal's salesperson
    (``plan.submitted_by``, fallback the requisition creator). Channel policy:
    - In-app ``ActivityLog`` row + Teams channel card: ALWAYS (delivery floor); the card is
      gated only by webhook presence.
    - Email + Teams DM: only to recipients whose ``notify_resource_alert_enabled`` is True.

    Failure isolation: the in-app rows are committed BEFORE any Teams call, and each channel
    swallows + logs its own errors, so Teams being down can't lose the in-app record or
    block email. Dispatched fire-and-forget via ``run_notify_bg(notify_resource_requested,
    plan_id, line_id=..., actor_id=..., reason=...)`` \u2014 re-derives everything from line_id.
    """
    line = db.get(BuyPlanLine, line_id)
    if not line:
        logger.warning("Re-source: line {} not found for plan {}, skipping notify", line_id, plan.id)
        return

    offer = line.offer
    vendor_card_id = offer.vendor_card_id if offer else None
    rc = _resource_context(plan, line, db, reason)

    # \u2500\u2500 Recipients: active PO-cutters (buyer/manager/admin) minus the actor + the
    #    salesperson. Managers/admins are included because they can ALSO claim the open
    #    pool \u2014 so if every buyer is the actor/inactive they remain reachable. \u2500\u2500
    recipients = (
        db.query(User)
        .filter(
            User.role.in_([UserRole.BUYER, UserRole.MANAGER, UserRole.ADMIN]),
            User.is_active.is_(True),
            User.id != actor_id,
        )
        .all()
    )
    seen = {u.id for u in recipients}
    salesperson = plan.submitted_by or (plan.requisition.creator if plan.requisition else None)
    if salesperson and salesperson.id != actor_id and salesperson.id not in seen:
        recipients.append(salesperson)
        seen.add(salesperson.id)

    if not recipients:
        # URGENT alert reaching nobody is a real operational gap, not routine.
        logger.warning("Re-source: URGENT alert for plan {} reached 0 recipients (actor {})", plan.id, actor_id)
        return

    # \u2500\u2500 In-app rows (ALWAYS, every recipient) \u2014 committed before Teams \u2500\u2500
    subject = f"URGENT \u2014 Re-source needed: {rc['mpn']} (plan #{plan.id})"
    notes = f"{rc['vendor']} PO cancelled: {rc['reason']} \u2014 {rc['deep_link']}"
    for r in recipients:
        db.add(
            ActivityLog(
                user_id=r.id,
                activity_type=ActivityType.RESOURCE_REQUESTED,
                channel="system",
                requisition_id=plan.requisition_id,
                requirement_id=line.requirement_id,
                vendor_card_id=vendor_card_id,
                buy_plan_id=plan.id,
                subject=subject,
                notes=notes,
            )
        )
    db.commit()

    # \u2500\u2500 Teams channel card (ALWAYS; gated only by webhook presence) \u2500\u2500
    try:
        await _teams_channel_card(_resource_card(rc))
    except Exception as e:
        logger.error("Re-source Teams channel card failed for plan {}: {}", plan.id, e)

    # \u2500\u2500 Email + Teams DM: only to opted-in recipients \u2500\u2500
    opted_in = [r for r in recipients if getattr(r, "notify_resource_alert_enabled", True)]
    html_body = _resource_email_html(rc)
    email_subject = f"[AVAIL] URGENT \u2014 Re-source needed: {rc['customer']}"
    await asyncio.gather(
        *[_send_email(r, email_subject, html_body, db, pref_attr="notify_resource_alert_enabled") for r in opted_in]
    )

    dm_text = _resource_dm_text(rc)
    for r in opted_in:
        try:
            await _teams_dm(r, dm_text, db)
        except Exception as e:
            logger.error("Re-source Teams DM to {} failed: {}", r.email, e)
