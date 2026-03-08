"""Buy Plan V1 — Email, Teams, and in-app notifications for buy plan lifecycle.

Handles: submit, approve, reject, stock sale, complete, cancel notifications.
All notifications go through email (Graph API), Teams (webhook/DM), and in-app (ActivityLog).

Called by: routers/crm/buy_plans.py (via buyplan_service façade)
Depends on: utils/graph_client, teams_notifications, models, config, scheduler
"""

import asyncio
import html

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ActivityLog, BuyPlan, Offer, User

# ── Background Task Helper ─────────────────────────────────────────────


def run_buyplan_bg(coro_factory, plan_id: int, **kwargs):
    """Fire-and-forget a buyplan coroutine in a dedicated DB session.

    Replaces the repeated inline async-def + create_task pattern throughout
    crm.py.  ``coro_factory`` is an async callable ``(plan, db, **kw)``.
    """

    async def _run():
        from ..database import SessionLocal

        bg_db = SessionLocal()
        try:
            bg_plan = bg_db.get(BuyPlan, plan_id)
            if bg_plan:
                await coro_factory(bg_plan, bg_db, **kwargs)
        except Exception:
            logger.exception(
                "Background %s failed for plan %s",
                coro_factory.__name__,
                plan_id,
            )
        finally:
            bg_db.close()

    asyncio.create_task(_run())


# ── Audit Trail ─────────────────────────────────────────────────────────


def log_buyplan_activity(
    db: Session,
    user_id: int,
    plan: "BuyPlan",
    activity_type: str,
    detail: str = "",
):
    """Create an ActivityLog entry for a buy plan state change."""
    db.add(
        ActivityLog(
            user_id=user_id,
            activity_type=activity_type,
            channel="system",
            requisition_id=plan.requisition_id,
            # buy_plan_id FK targets buy_plans_v3; V1 plans use notes for linkage
            subject=f"Buy plan #{plan.id}: {detail}" if detail else f"Buy plan #{plan.id}",
            notes=f"plan_id={plan.id} status={plan.status}",
        )
    )


# ── Teams Integration (unified Graph API Adaptive Cards) ─────────────────


async def _post_teams_card(
    plan: "BuyPlan", event: str, subtitle: str, facts: list[dict], admin_mentions: list[tuple[str, str]] | None = None
):
    """Post a buy plan event as an Adaptive Card via Graph API.

    Replaces the old webhook-based plain text posts with rich Adaptive Cards.
    """
    from app.services.teams import send_buyplan_approval_card, send_buyplan_card

    if event == "buyplan_submitted":
        total_cost = sum(
            (i.get("plan_qty") or i.get("qty") or 0) * (i.get("cost_price") or 0) for i in (plan.line_items or [])
        )
        await send_buyplan_approval_card(
            plan_id=plan.id,
            submitter_name=subtitle,
            total_cost=total_cost,
            line_count=len(plan.line_items or []),
            requisition_id=plan.requisition_id,
            admin_emails=admin_mentions,
        )
    else:
        await send_buyplan_card(
            plan_id=plan.id,
            event=event,
            subtitle=subtitle,
            facts=facts,
            mention_emails=admin_mentions,
        )


async def _send_teams_dm(user: User, message: str, db: Session = None):
    """Send a direct Teams message to a user via Graph API.

    Delegates to shared teams_notifications module.
    """
    from app.services.teams_notifications import send_teams_dm

    await send_teams_dm(user, message, db)


# ── Email Notifications ──────────────────────────────────────────────────


async def notify_buyplan_submitted(plan: BuyPlan, db: Session):
    """Notify admins that a buy plan needs approval."""
    from ..models import Quote
    from ..scheduler import get_valid_token

    submitter = db.get(User, plan.submitted_by_id)
    submitter_name = submitter.name or submitter.email if submitter else "Unknown"

    # Deal context
    customer_name = ""
    quote_number = ""
    quote = db.get(Quote, plan.quote_id) if plan.quote_id else None
    if quote and quote.customer_site and quote.customer_site.company:
        customer_name = f"{quote.customer_site.company.name} — {quote.customer_site.site_name}"
        quote_number = quote.quote_number or ""

    # Build line items table
    rows = ""
    total_cost = 0
    for item in plan.line_items or []:
        plan_qty = item.get("plan_qty") or item.get("qty") or 0
        cost = plan_qty * (item.get("cost_price") or 0)
        total_cost += cost
        rows += f"""<tr>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("mpn", "")))}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("vendor_name", "")))}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{plan_qty:,}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">${item.get("cost_price", 0):.4f}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">${cost:,.2f}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("lead_time", "")))}</td>
        </tr>"""

    sp_notes_html = ""
    if plan.salesperson_notes:
        sp_notes_html = f'<p style="background:#f0f9ff;padding:10px;border-left:3px solid #2563eb;margin:12px 0"><strong>Salesperson Notes:</strong> {html.escape(str(plan.salesperson_notes))}</p>'

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:700px">
        <h2 style="color:#2563eb">Buy Plan Approval Required</h2>
        <p><strong>{html.escape(str(submitter_name))}</strong> has submitted a buy plan for approval.</p>
        <p>Customer: <strong>{html.escape(str(customer_name))}</strong></p>
        <p>Requisition: <strong>#{plan.requisition_id}</strong> | Quote: <strong>{html.escape(str(quote_number))}</strong></p>
        {sp_notes_html}
        <table style="border-collapse:collapse;width:100%;margin:16px 0">
            <thead><tr style="background:#f3f4f6">
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">MPN</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Vendor</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Plan Qty</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Unit Cost</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Line Total</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Lead Time</th>
            </tr></thead>
            <tbody>{rows}</tbody>
            <tfoot><tr style="background:#f3f4f6;font-weight:bold">
                <td colspan="4" style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Total</td>
                <td style="padding:8px 10px;border:1px solid #e5e7eb">${total_cost:,.2f}</td>
                <td style="padding:8px 10px;border:1px solid #e5e7eb"></td>
            </tr></tfoot>
        </table>
        <p style="margin-top:20px">
            <a href="{settings.app_url}/#buyplan/{plan.id}"
               style="background:#2563eb;color:white;padding:10px 24px;text-decoration:none;border-radius:5px;margin-right:8px">
                Review & Approve
            </a>
            <a href="{settings.app_url}/#approve-token/{plan.approval_token}"
               style="background:#16a34a;color:white;padding:10px 24px;text-decoration:none;border-radius:5px">
                Quick Approve
            </a>
        </p>
        <p style="color:#6b7280;font-size:12px;margin-top:20px">
            This is an automated alert from AVAIL. Log in to review, edit, or reject.
        </p>
    </div>
    """

    # Send email to all admins in parallel
    admin_users = db.query(User).filter(User.email.in_(settings.admin_emails)).all()

    async def _send_admin_email(admin):
        try:
            token = await get_valid_token(admin, db)
            if not token:
                return
            from ..utils.graph_client import GraphClient

            gc = GraphClient(token)
            await gc.post_json(
                "/me/sendMail",
                {
                    "message": {
                        "subject": f"[AVAIL] Buy Plan Approval Required — #{plan.requisition_id}",
                        "body": {"contentType": "HTML", "content": html_body},
                        "toRecipients": [{"emailAddress": {"address": admin.email}}],
                    },
                    "saveToSentItems": "false",
                },
            )
            logger.info(f"Buy plan email sent to admin {admin.email}")
        except Exception as e:
            logger.error(f"Failed to send buy plan email to {admin.email}: {e}")

    await asyncio.gather(*[_send_admin_email(a) for a in admin_users])

    # In-app notification
    for admin in admin_users:
        db.add(
            ActivityLog(
                user_id=admin.id,
                activity_type="buyplan_pending",
                channel="system",
                # buy_plan_id FK targets buy_plans_v3; V1 plans use notes for linkage
                subject=f"Buy plan #{plan.id} awaiting approval — {submitter_name}",
            )
        )
    db.commit()

    # Teams notification — Adaptive Card with Approve/Reject buttons
    admin_mentions = [(a.email, a.name or a.email) for a in admin_users]
    await _post_teams_card(plan, "buyplan_submitted", submitter_name, [], admin_mentions)
    await asyncio.gather(
        *[
            _send_teams_dm(admin, f"Buy Plan #{plan.id} needs your approval — ${total_cost:,.2f}", db)
            for admin in admin_users
        ]
    )


async def notify_buyplan_approved(plan: BuyPlan, db: Session):
    """Notify buyers that their offers need to be purchased."""
    from ..models import Quote
    from ..scheduler import get_valid_token

    approver = db.get(User, plan.approved_by_id)
    approver_name = approver.name or approver.email if approver else "Manager"

    # Deal context
    customer_name = ""
    quote_number = ""
    quote = db.get(Quote, plan.quote_id) if plan.quote_id else None
    if quote and quote.customer_site and quote.customer_site.company:
        customer_name = f"{quote.customer_site.company.name} — {quote.customer_site.site_name}"
        quote_number = quote.quote_number or ""

    so_number = plan.sales_order_number or "N/A"

    # Identify unique buyers from the line items
    buyer_ids = set()
    for item in plan.line_items or []:
        entered_by = item.get("entered_by_id")
        if entered_by:
            buyer_ids.add(entered_by)
    if not buyer_ids:
        offer_ids = [item.get("offer_id") for item in (plan.line_items or []) if item.get("offer_id")]
        if offer_ids:
            offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
            buyer_ids = {o.entered_by_id for o in offers if o.entered_by_id}

    buyers = db.query(User).filter(User.id.in_(buyer_ids)).all() if buyer_ids else []

    async def _notify_buyer(buyer):
        buyer_items = [i for i in (plan.line_items or []) if i.get("entered_by_id") == buyer.id]
        if not buyer_items:
            buyer_items = plan.line_items or []

        rows = ""
        for item in buyer_items:
            plan_qty = item.get("plan_qty") or item.get("qty") or 0
            rows += f"""<tr>
                <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("mpn", "")))}</td>
                <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("vendor_name", "")))}</td>
                <td style="padding:6px 10px;border:1px solid #e5e7eb">{plan_qty:,}</td>
                <td style="padding:6px 10px;border:1px solid #e5e7eb">${item.get("cost_price", 0):.4f}</td>
                <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("lead_time", "")))}</td>
            </tr>"""

        notes_html = ""
        if plan.salesperson_notes:
            notes_html += f'<p style="background:#f0f9ff;padding:10px;border-left:3px solid #2563eb;margin:8px 0"><strong>Salesperson Notes:</strong> {html.escape(str(plan.salesperson_notes))}</p>'
        if plan.manager_notes:
            notes_html += f'<p style="background:#f0fdf4;padding:10px;border-left:3px solid #16a34a;margin:8px 0"><strong>Manager Notes:</strong> {html.escape(str(plan.manager_notes))}</p>'

        html_body = f"""
        <div style="font-family:Arial,sans-serif;max-width:700px">
            <h2 style="color:#16a34a">Buy Plan Approved — PO Required</h2>
            <p>Approved by <strong>{html.escape(str(approver_name))}</strong>. Please create POs in Acctivate and enter the PO numbers in AVAIL.</p>
            <div style="background:#f3f4f6;padding:12px;border-radius:6px;margin:12px 0">
                <p style="margin:0"><strong>Customer:</strong> {html.escape(str(customer_name))}</p>
                <p style="margin:4px 0 0"><strong>Acctivate SO#:</strong> {html.escape(str(so_number))}</p>
                <p style="margin:4px 0 0"><strong>Quote:</strong> {html.escape(str(quote_number))}</p>
            </div>
            {notes_html}
            <table style="border-collapse:collapse;width:100%;margin:16px 0">
                <thead><tr style="background:#f3f4f6">
                    <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">MPN</th>
                    <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Vendor</th>
                    <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Plan Qty</th>
                    <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Unit Cost</th>
                    <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Lead Time</th>
                </tr></thead>
                <tbody>{rows}</tbody>
            </table>
            <p style="margin-top:20px">
                <a href="{settings.app_url}/#buyplan/{plan.id}"
                   style="background:#16a34a;color:white;padding:10px 24px;text-decoration:none;border-radius:5px">
                    Enter PO Numbers
                </a>
            </p>
        </div>
        """

        try:
            token = await get_valid_token(buyer, db)
            if not token:
                return
            from ..utils.graph_client import GraphClient

            gc = GraphClient(token)
            await gc.post_json(
                "/me/sendMail",
                {
                    "message": {
                        "subject": f"[AVAIL] Buy Plan Approved — PO Required for #{plan.requisition_id}",
                        "body": {"contentType": "HTML", "content": html_body},
                        "toRecipients": [{"emailAddress": {"address": buyer.email}}],
                    },
                    "saveToSentItems": "false",
                },
            )
            logger.info(f"Buy plan approved email sent to buyer {buyer.email}")
        except Exception as e:
            logger.error(f"Failed to send approved email to {buyer.email}: {e}")

        # In-app notification
        db.add(
            ActivityLog(
                user_id=buyer.id,
                activity_type="buyplan_approved",
                channel="system",
                # buy_plan_id FK targets buy_plans_v3; V1 plans use notes for linkage
                subject=f"Buy plan #{plan.id} approved — create POs",
            )
        )

        # Teams DM
        await _send_teams_dm(
            buyer,
            f"Buy Plan #{plan.id} has been approved. Please create POs for {len(buyer_items)} item(s) in Acctivate.",
            db,
        )

    await asyncio.gather(*[_notify_buyer(b) for b in buyers])
    db.commit()

    # Teams channel post — Adaptive Card
    await _post_teams_card(
        plan,
        "buyplan_approved",
        f"Approved by {approver_name}",
        [
            {"title": "Approver", "value": approver_name},
            {"title": "Buyers Notified", "value": ", ".join(b.name or b.email for b in buyers)},
            {"title": "SO#", "value": so_number},
        ],
    )


async def notify_buyplan_rejected(plan: BuyPlan, db: Session):
    """Notify the salesperson that their buy plan was rejected."""
    from ..scheduler import get_valid_token

    submitter = db.get(User, plan.submitted_by_id)
    if not submitter:
        return

    rejector = db.get(User, plan.approved_by_id)
    rejector_name = rejector.name or rejector.email if rejector else "Manager"

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px">
        <h2 style="color:#dc2626">Buy Plan Rejected</h2>
        <p>Your buy plan for requisition <strong>#{plan.requisition_id}</strong> was rejected by <strong>{html.escape(str(rejector_name))}</strong>.</p>
        {f"<p><strong>Reason:</strong> {html.escape(str(plan.rejection_reason))}</p>" if plan.rejection_reason else ""}
        <p style="margin-top:20px">
            <a href="{settings.app_url}/#buyplan/{plan.id}"
               style="background:#2563eb;color:white;padding:10px 24px;text-decoration:none;border-radius:5px">
                View Details
            </a>
        </p>
    </div>
    """

    try:
        token = await get_valid_token(submitter, db)
        if token:
            from ..utils.graph_client import GraphClient

            gc = GraphClient(token)
            await gc.post_json(
                "/me/sendMail",
                {
                    "message": {
                        "subject": f"[AVAIL] Buy Plan Rejected — #{plan.requisition_id}",
                        "body": {"contentType": "HTML", "content": html_body},
                        "toRecipients": [{"emailAddress": {"address": submitter.email}}],
                    },
                    "saveToSentItems": "false",
                },
            )
    except Exception as e:
        logger.error(f"Failed to send rejection email to {submitter.email}: {e}")

    db.add(
        ActivityLog(
            user_id=submitter.id,
            activity_type="buyplan_rejected",
            channel="system",
            # buy_plan_id FK targets buy_plans_v3; V1 plans use notes for linkage
            subject=f"Buy plan #{plan.id} rejected — {plan.rejection_reason or 'no reason given'}",
        )
    )
    db.commit()

    await _send_teams_dm(
        submitter,
        f"Buy Plan #{plan.id} was rejected: {plan.rejection_reason or 'no reason given'}",
        db,
    )


async def notify_stock_sale_approved(plan: BuyPlan, db: Session):
    """Notify logistics/accounting that a stock sale was approved (no PO required)."""
    from ..scheduler import get_valid_token

    approver = db.get(User, plan.approved_by_id) if plan.approved_by_id else None
    approver_name = (approver.name or approver.email) if approver else "Manager (email token)"

    submitter = db.get(User, plan.submitted_by_id)
    submitter_name = submitter.name or submitter.email if submitter else "Unknown"

    # Build line items table
    rows = ""
    total_cost = 0
    for item in plan.line_items or []:
        plan_qty = item.get("plan_qty") or item.get("qty") or 0
        cost = plan_qty * (item.get("cost_price") or 0)
        total_cost += cost
        rows += f"""<tr>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("mpn", "")))}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("vendor_name", "")))}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{plan_qty:,}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">${item.get("cost_price", 0):.4f}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">${cost:,.2f}</td>
        </tr>"""

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:700px">
        <h2 style="color:#7c3aed">Stock Sale Approved — No PO Required</h2>
        <p>Approved by <strong>{html.escape(str(approver_name))}</strong>.</p>
        <p>This is an internal stock sale — no purchase orders are needed.</p>
        <div style="background:#f3f4f6;padding:12px;border-radius:6px;margin:12px 0">
            <p style="margin:0"><strong>Submitted by:</strong> {html.escape(str(submitter_name))}</p>
            <p style="margin:4px 0 0"><strong>Acctivate SO#:</strong> {html.escape(str(plan.sales_order_number or "N/A"))}</p>
        </div>
        <table style="border-collapse:collapse;width:100%;margin:16px 0">
            <thead><tr style="background:#f3f4f6">
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">MPN</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Vendor</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Plan Qty</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Unit Cost</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Line Total</th>
            </tr></thead>
            <tbody>{rows}</tbody>
            <tfoot><tr style="background:#f3f4f6;font-weight:bold">
                <td colspan="4" style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Total</td>
                <td style="padding:8px 10px;border:1px solid #e5e7eb">${total_cost:,.2f}</td>
            </tr></tfoot>
        </table>
        <p style="margin-top:20px">
            <a href="{settings.app_url}/#buyplan/{plan.id}"
               style="background:#7c3aed;color:white;padding:10px 24px;text-decoration:none;border-radius:5px">
                View in AVAIL
            </a>
        </p>
    </div>
    """

    # Send to stock_sale_notify_emails using an admin user's token
    admin_users = db.query(User).filter(User.email.in_(settings.admin_emails)).all()
    sender = next((a for a in admin_users if a.access_token), None)
    if sender:
        token = await get_valid_token(sender, db)
        if token:
            from ..utils.graph_client import GraphClient

            gc = GraphClient(token)

            async def _send_stock_email(email_addr):
                try:
                    await gc.post_json(
                        "/me/sendMail",
                        {
                            "message": {
                                "subject": f"[AVAIL] Stock Sale Approved — #{plan.requisition_id}",
                                "body": {"contentType": "HTML", "content": html_body},
                                "toRecipients": [{"emailAddress": {"address": email_addr}}],
                            },
                            "saveToSentItems": "false",
                        },
                    )
                    logger.info(f"Stock sale email sent to {email_addr}")
                except Exception as e:
                    logger.error(f"Failed to send stock sale email to {email_addr}: {e}")

            await asyncio.gather(*[_send_stock_email(e) for e in settings.stock_sale_notify_emails])

    # In-app notification to submitter
    if submitter:
        db.add(
            ActivityLog(
                user_id=submitter.id,
                activity_type="buyplan_completed",
                channel="system",
                # buy_plan_id FK targets buy_plans_v3; V1 plans use notes for linkage
                subject=f"Stock sale #{plan.id} approved and completed — no PO required",
            )
        )
    db.commit()

    # Teams channel post — Adaptive Card
    await _post_teams_card(
        plan,
        "buyplan_completed",
        f"Stock sale approved by {approver_name}",
        [
            {"title": "Approver", "value": approver_name},
            {"title": "Submitter", "value": submitter_name},
            {"title": "Total", "value": f"${total_cost:,.2f}"},
            {"title": "Type", "value": "Stock Sale (no PO required)"},
        ],
    )


async def notify_buyplan_completed(plan: BuyPlan, db: Session, completer_name: str):
    """Notify the original submitter that their buy plan is complete."""
    from ..scheduler import get_valid_token

    submitter = db.get(User, plan.submitted_by_id)
    if not submitter:
        return

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px">
        <h2 style="color:#16a34a">Buy Plan Complete</h2>
        <p>Your buy plan for requisition <strong>#{plan.requisition_id}</strong>
           has been marked complete by <strong>{html.escape(str(completer_name))}</strong>.</p>
        <p>Sales Order: <strong>{html.escape(str(plan.sales_order_number or "N/A"))}</strong></p>
        <p style="margin-top:20px">
            <a href="{settings.app_url}/#buyplan/{plan.id}"
               style="background:#16a34a;color:white;padding:10px 24px;text-decoration:none;border-radius:5px">
                View Details
            </a>
        </p>
    </div>
    """

    try:
        token = await get_valid_token(submitter, db)
        if token:
            from ..utils.graph_client import GraphClient

            gc = GraphClient(token)
            await gc.post_json(
                "/me/sendMail",
                {
                    "message": {
                        "subject": f"[AVAIL] Buy Plan Complete — #{plan.requisition_id}",
                        "body": {"contentType": "HTML", "content": html_body},
                        "toRecipients": [{"emailAddress": {"address": submitter.email}}],
                    },
                    "saveToSentItems": "false",
                },
            )
    except Exception as e:
        logger.error(f"Failed to send completion email to {submitter.email}: {e}")

    db.add(
        ActivityLog(
            user_id=submitter.id,
            activity_type="buyplan_completed",
            channel="system",
            # buy_plan_id FK targets buy_plans_v3; V1 plans use notes for linkage
            subject=f"Buy plan #{plan.id} completed",
        )
    )
    db.commit()

    await _post_teams_card(
        plan,
        "buyplan_completed",
        f"Completed by {completer_name}",
        [
            {"title": "Completed By", "value": completer_name},
            {"title": "Requisition", "value": f"#{plan.requisition_id}"},
        ],
    )


async def notify_buyplan_cancelled(plan: BuyPlan, db: Session):
    """Notify relevant parties about cancellation."""
    canceller = db.get(User, plan.cancelled_by_id)
    canceller_name = canceller.name or canceller.email if canceller else "Unknown"

    # If the canceller is the submitter, notify admins. Otherwise notify submitter.
    if plan.cancelled_by_id == plan.submitted_by_id:
        targets = db.query(User).filter(User.email.in_(settings.admin_emails)).all()
    else:
        submitter = db.get(User, plan.submitted_by_id)
        targets = [submitter] if submitter else []

    reason_text = f" — {plan.cancellation_reason}" if plan.cancellation_reason else ""
    for target in targets:
        db.add(
            ActivityLog(
                user_id=target.id,
                activity_type="buyplan_cancelled",
                channel="system",
                # buy_plan_id FK targets buy_plans_v3; V1 plans use notes for linkage
                subject=f"Buy plan #{plan.id} cancelled by {canceller_name}{reason_text}",
            )
        )
    db.commit()

    await _post_teams_card(
        plan,
        "buyplan_cancelled",
        f"Cancelled by {canceller_name}",
        [
            {"title": "Cancelled By", "value": canceller_name},
            {"title": "Reason", "value": plan.cancellation_reason or "No reason given"},
        ],
    )
