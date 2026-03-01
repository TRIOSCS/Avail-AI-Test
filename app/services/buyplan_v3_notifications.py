"""
buyplan_v3_notifications.py — Buy Plan V3 notification service.

Handles notifications for all V3 buy plan state transitions:
- Submit  → email + Teams + in-app to managers
- Approve → email + Teams + in-app to buyers + salesperson
- Reject  → email + in-app to salesperson
- SO Verified → in-app to buyers
- SO Rejected/Halted → email + in-app to salesperson
- PO Confirmed → in-app to ops
- Issue Flagged → in-app + Teams to manager
- Completed → email + Teams + in-app to salesperson
- Resubmit → email + in-app to managers

Called by: routers/crm/buy_plans_v3.py
Depends on: models, config, utils/graph_client, buyplan_service (_post_teams_channel, _send_teams_dm)
"""

import asyncio
import html as html_mod

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ActivityLog, User
from ..models.buy_plan import BuyPlanV3

# ── Background runner ────────────────────────────────────────────────


def run_v3_notify_bg(coro_factory, plan_id: int, **kwargs):
    """Fire-and-forget a V3 notification coroutine with its own DB session."""

    async def _run():
        from ..database import SessionLocal

        bg_db = SessionLocal()
        try:
            bg_plan = bg_db.get(BuyPlanV3, plan_id)
            if bg_plan:
                await coro_factory(bg_plan, bg_db, **kwargs)
        except Exception:
            logger.exception("Background %s failed for V3 plan %s", coro_factory.__name__, plan_id)
        finally:
            bg_db.close()

    asyncio.create_task(_run())


# ── Helpers ──────────────────────────────────────────────────────────


def _plan_context(plan: BuyPlanV3, db: Session) -> dict:
    """Extract common context fields from a V3 plan."""
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
    return {
        "submitter": submitter,
        "submitter_name": submitter.name or submitter.email if submitter else "Unknown",
        "customer_name": customer_name,
        "quote_number": quote_number,
    }


def _lines_html(plan: BuyPlanV3) -> tuple[str, float]:
    """Build HTML table rows for plan lines. Returns (rows_html, total_cost)."""
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


def _wrap_email(title: str, body_inner: str) -> str:
    """Wrap content in the standard AVAIL email template."""
    return (
        f'<div style="font-family:Arial,sans-serif;max-width:700px">'
        f'<h2 style="color:#2563eb">{html_mod.escape(title)}</h2>'
        f"{body_inner}"
        f'<p style="color:#6b7280;font-size:12px;margin-top:20px">'
        f"This is an automated alert from AVAIL.</p></div>"
    )


async def _send_email(user: User, subject: str, html_body: str, db: Session):
    """Send an email to a single user via Graph API."""
    from ..scheduler import get_valid_token
    from ..utils.graph_client import GraphClient

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
        logger.info("V3 buy plan email sent to %s", user.email)
    except Exception as e:
        logger.error("Failed to send V3 buy plan email to %s: %s", user.email, e)


# ── Reuse V1 Teams helpers ───────────────────────────────────────────


async def _teams_channel(message: str):
    """Post to Teams channel (delegates to V1 helper)."""
    from .buyplan_service import _post_teams_channel

    await _post_teams_channel(message)


async def _teams_dm(user: User, message: str, db: Session):
    """Send Teams DM (delegates to V1 helper)."""
    from .buyplan_service import _send_teams_dm

    await _send_teams_dm(user, message, db)


# ── Notification Functions ───────────────────────────────────────────


async def notify_v3_submitted(plan: BuyPlanV3, db: Session):
    """Notify managers that a V3 buy plan needs approval."""
    ctx = _plan_context(plan, db)
    rows, total = _lines_html(plan)

    notes_html = ""
    if plan.salesperson_notes:
        notes_html = f'<p style="background:#f0f9ff;padding:10px;border-left:3px solid #2563eb;margin:12px 0"><strong>Sales Notes:</strong> {html_mod.escape(str(plan.salesperson_notes))}</p>'

    body = (
        f"<p><strong>{html_mod.escape(ctx['submitter_name'])}</strong> submitted a buy plan.</p>"
        f"<p>Customer: <strong>{html_mod.escape(ctx['customer_name'])}</strong> | "
        f"Quote: <strong>{html_mod.escape(ctx['quote_number'])}</strong> | "
        f"SO#: <strong>{html_mod.escape(plan.sales_order_number or '')}</strong></p>"
        f"{notes_html}"
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
    html_body = _wrap_email("Buy Plan V3 — Approval Required", body)

    # Email to managers/admins
    managers = db.query(User).filter(User.role.in_(["manager", "admin"])).all()
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
                activity_type="buyplan_pending",
                channel="system",
                requisition_id=plan.requisition_id,
                subject=f"Buy plan V3 #{plan.id} needs approval — {ctx['submitter_name']}",
            )
        )
    db.commit()

    # Teams
    await _teams_channel(
        f"**Buy Plan V3 #{plan.id} — Approval Required**\n\n"
        f"Submitted by: {ctx['submitter_name']}\n"
        f"Customer: {ctx['customer_name']} | SO#: {plan.sales_order_number or '—'}\n"
        f"Total: ${total:,.2f} | {len(plan.lines or [])} lines"
    )


async def notify_v3_approved(plan: BuyPlanV3, db: Session):
    """Notify buyers and salesperson that the plan was approved."""
    ctx = _plan_context(plan, db)
    rows, total = _lines_html(plan)

    # Collect unique buyers
    buyer_ids = {ln.buyer_id for ln in (plan.lines or []) if ln.buyer_id}
    buyers = [db.get(User, bid) for bid in buyer_ids]
    buyers = [b for b in buyers if b]

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
                subject=f"Your buy plan #{plan.id} was approved",
            )
        )

    db.commit()

    await _teams_channel(
        f"**Buy Plan V3 #{plan.id} — Approved**\n\n"
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


async def notify_v3_rejected(plan: BuyPlanV3, db: Session):
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


async def notify_v3_so_verified(plan: BuyPlanV3, db: Session):
    """Notify buyers that SO has been verified — they can proceed."""
    buyer_ids = {ln.buyer_id for ln in (plan.lines or []) if ln.buyer_id}
    for bid in buyer_ids:
        db.add(
            ActivityLog(
                user_id=bid,
                activity_type="buyplan_approved",
                channel="system",
                requisition_id=plan.requisition_id,
                subject=f"SO# {plan.sales_order_number} verified — proceed with POs (plan #{plan.id})",
            )
        )
    db.commit()


async def notify_v3_so_rejected(plan: BuyPlanV3, db: Session, action: str):
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
            subject=f"SO# {plan.sales_order_number} {label} — plan #{plan.id}",
        )
    )
    db.commit()


async def notify_v3_issue_flagged(plan: BuyPlanV3, db: Session, line_id: int, issue_type: str):
    """Notify managers when a buyer flags an issue on a line."""
    from ..models.buy_plan import BuyPlanLine

    line = db.get(BuyPlanLine, line_id)
    mpn = line.offer.mpn if line and line.offer else "—"

    managers = db.query(User).filter(User.role.in_(["manager", "admin"])).all()
    if not managers:
        managers = db.query(User).filter(User.email.in_(settings.admin_emails)).all()

    issue_labels = {
        "sold_out": "Sold Out",
        "price_changed": "Price Changed",
        "lead_time_changed": "Lead Time Changed",
        "other": "Other",
    }
    label = issue_labels.get(issue_type, issue_type)

    for m in managers:
        db.add(
            ActivityLog(
                user_id=m.id,
                activity_type="buyplan_pending",
                channel="system",
                requisition_id=plan.requisition_id,
                subject=f"Issue on plan #{plan.id}: {mpn} — {label}",
            )
        )
    db.commit()

    await _teams_channel(
        f"**Buy Plan #{plan.id} — Issue Flagged**\n\nMPN: {mpn} | Issue: {label}\nAction may be required."
    )


async def notify_v3_po_confirmed(plan: BuyPlanV3, db: Session, line_id: int):
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
                activity_type="buyplan_pending",
                channel="system",
                requisition_id=plan.requisition_id,
                subject=f"PO {po_num} needs verification — {mpn} (plan #{plan.id})",
            )
        )
    db.commit()


async def notify_v3_completed(plan: BuyPlanV3, db: Session):
    """Notify salesperson that the plan is complete."""
    ctx = _plan_context(plan, db)
    if not ctx["submitter"]:
        return

    _, total = _lines_html(plan)
    body = (
        f"<p>Buy plan #{plan.id} is now <strong>complete</strong>.</p>"
        f"<p>Customer: <strong>{html_mod.escape(ctx['customer_name'])}</strong> | "
        f"SO#: <strong>{html_mod.escape(plan.sales_order_number or '')}</strong> | "
        f"Total: <strong>${total:,.2f}</strong></p>"
    )
    html_body = _wrap_email("Buy Plan Completed", body)
    await _send_email(ctx["submitter"], f"[AVAIL] Buy Plan Complete — {ctx['customer_name']}", html_body, db)

    db.add(
        ActivityLog(
            user_id=ctx["submitter"].id,
            activity_type="buyplan_completed",
            channel="system",
            requisition_id=plan.requisition_id,
            subject=f"Buy plan #{plan.id} completed — {ctx['customer_name']}",
        )
    )
    db.commit()

    await _teams_channel(
        f"**Buy Plan V3 #{plan.id} — Completed**\n\n"
        f"Customer: {ctx['customer_name']} | SO#: {plan.sales_order_number or '—'} | ${total:,.2f}"
    )
