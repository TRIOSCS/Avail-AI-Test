"""
buyplan_service.py — Buy Plan notifications, Teams integration, and PO verification.

Handles the full buy plan lifecycle notifications:
- Submit → email + Teams + in-app to admins
- Approve → email + Teams + in-app to buyers
- Reject → email + Teams + in-app to salesperson
- PO verification → scan buyer's sent emails for PO number

Called by: routers/crm.py (buy plan endpoints)
Depends on: utils/graph_client, models, config
"""

import html
import logging

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ActivityLog, BuyPlan, Offer, User

log = logging.getLogger("avail.buyplan")


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
            subject=f"Buy plan #{plan.id}: {detail}" if detail else f"Buy plan #{plan.id}",
            notes=f"plan_id={plan.id} status={plan.status}",
        )
    )


# ── Email Notifications ──────────────────────────────────────────────────


async def notify_buyplan_submitted(plan: BuyPlan, db: Session):
    """Notify admins that a buy plan needs approval."""
    from ..scheduler import get_valid_token
    from ..models import Quote

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

    # Send email to each admin
    admin_users = db.query(User).filter(User.email.in_(settings.admin_emails)).all()
    for admin in admin_users:
        try:
            token = await get_valid_token(admin, db)
            if not token:
                continue
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
            log.info(f"Buy plan email sent to admin {admin.email}")
        except Exception as e:
            log.error(f"Failed to send buy plan email to {admin.email}: {e}")

    # In-app notification
    for admin in admin_users:
        db.add(
            ActivityLog(
                user_id=admin.id,
                activity_type="buyplan_pending",
                channel="system",
                subject=f"Buy plan #{plan.id} awaiting approval — {submitter_name}",
            )
        )
    db.commit()

    # Teams notification
    await _post_teams_channel(
        f"**Buy Plan #{plan.id} — Approval Required**\n\n"
        f"Submitted by: {submitter_name}\n"
        f"Total: ${total_cost:,.2f} | {len(plan.line_items or [])} line items\n\n"
        f"[Review in AVAIL]({settings.app_url}/#buyplan/{plan.id})"
    )
    for admin in admin_users:
        await _send_teams_dm(
            admin, f"Buy Plan #{plan.id} needs your approval — ${total_cost:,.2f}", db
        )


async def notify_buyplan_approved(plan: BuyPlan, db: Session):
    """Notify buyers that their offers need to be purchased."""
    from ..scheduler import get_valid_token
    from ..models import Quote

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
        offer_ids = [
            item.get("offer_id")
            for item in (plan.line_items or [])
            if item.get("offer_id")
        ]
        if offer_ids:
            offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
            buyer_ids = {o.entered_by_id for o in offers if o.entered_by_id}

    buyers = db.query(User).filter(User.id.in_(buyer_ids)).all() if buyer_ids else []

    for buyer in buyers:
        buyer_items = [
            i for i in (plan.line_items or []) if i.get("entered_by_id") == buyer.id
        ]
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
                continue
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
            log.info(f"Buy plan approved email sent to buyer {buyer.email}")
        except Exception as e:
            log.error(f"Failed to send approved email to {buyer.email}: {e}")

        # In-app notification
        db.add(
            ActivityLog(
                user_id=buyer.id,
                activity_type="buyplan_approved",
                channel="system",
                subject=f"Buy plan #{plan.id} approved — create POs",
            )
        )

        # Teams DM
        await _send_teams_dm(
            buyer,
            f"Buy Plan #{plan.id} has been approved. "
            f"Please create POs for {len(buyer_items)} item(s) in Acctivate.",
            db,
        )

    db.commit()

    # Teams channel post
    await _post_teams_channel(
        f"**Buy Plan #{plan.id} — Approved** by {approver_name}\n\n"
        f"Buyers notified: {', '.join(b.name or b.email for b in buyers)}\n"
        f"[View in AVAIL]({settings.app_url}/#buyplan/{plan.id})"
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
                        "toRecipients": [
                            {"emailAddress": {"address": submitter.email}}
                        ],
                    },
                    "saveToSentItems": "false",
                },
            )
    except Exception as e:
        log.error(f"Failed to send rejection email to {submitter.email}: {e}")

    db.add(
        ActivityLog(
            user_id=submitter.id,
            activity_type="buyplan_rejected",
            channel="system",
            subject=f"Buy plan #{plan.id} rejected — {plan.rejection_reason or 'no reason given'}",
        )
    )
    db.commit()

    await _send_teams_dm(
        submitter,
        f"Buy Plan #{plan.id} was rejected: {plan.rejection_reason or 'no reason given'}",
        db,
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
        <p>Sales Order: <strong>{html.escape(str(plan.sales_order_number or 'N/A'))}</strong></p>
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
                        "toRecipients": [
                            {"emailAddress": {"address": submitter.email}}
                        ],
                    },
                    "saveToSentItems": "false",
                },
            )
    except Exception as e:
        log.error(f"Failed to send completion email to {submitter.email}: {e}")

    db.add(
        ActivityLog(
            user_id=submitter.id,
            activity_type="buyplan_completed",
            channel="system",
            subject=f"Buy plan #{plan.id} completed",
        )
    )
    db.commit()

    await _post_teams_channel(
        f"**Buy Plan #{plan.id} — Complete**\n\n"
        f"Completed by: {completer_name}\n"
        f"[View in AVAIL]({settings.app_url}/#buyplan/{plan.id})"
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
                subject=f"Buy plan #{plan.id} cancelled by {canceller_name}{reason_text}",
            )
        )
    db.commit()

    await _post_teams_channel(
        f"**Buy Plan #{plan.id} — Cancelled** by {canceller_name}\n\n"
        + (f"Reason: {plan.cancellation_reason}\n" if plan.cancellation_reason else "")
        + f"[View in AVAIL]({settings.app_url}/#buyplan/{plan.id})"
    )


# ── Teams Integration ────────────────────────────────────────────────────


async def _post_teams_channel(message: str):
    """Post a message to the configured Teams channel via webhook."""
    if not settings.teams_webhook_url:
        log.debug("Teams webhook not configured — skipping channel post")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                settings.teams_webhook_url,
                json={
                    "type": "message",
                    "attachments": [
                        {
                            "contentType": "application/vnd.microsoft.card.adaptive",
                            "content": {
                                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                                "type": "AdaptiveCard",
                                "version": "1.4",
                                "body": [
                                    {"type": "TextBlock", "text": message, "wrap": True}
                                ],
                            },
                        }
                    ],
                },
            )
            if resp.status_code not in (200, 202):
                log.warning(
                    f"Teams webhook returned {resp.status_code}: {resp.text[:200]}"
                )
    except Exception as e:
        log.error(f"Teams channel post failed: {e}")


async def _send_teams_dm(user: User, message: str, db: Session = None):
    """Send a direct Teams message to a user via Graph API."""
    if not user.access_token and not db:
        log.debug(f"No token for {user.email}, skipping Teams DM")
        return
    try:
        from ..utils.graph_client import GraphClient

        if db:
            from ..scheduler import get_valid_token

            token = await get_valid_token(user, db)
        else:
            token = user.access_token
        if not token:
            log.debug(f"No valid token for {user.email}, skipping Teams DM")
            return
        gc = GraphClient(token)
        # Create or get 1:1 chat with the user (self-chat acts as notification)
        chat = await gc.post_json(
            "/chats",
            {
                "chatType": "oneOnOne",
                "members": [
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{user.email}",
                    }
                ],
            },
        )
        chat_id = chat.get("id")
        if chat_id:
            await gc.post_json(
                f"/chats/{chat_id}/messages", {"body": {"content": message}}
            )
            log.info(f"Teams DM sent to {user.email}")
    except Exception as e:
        log.debug(
            f"Teams DM to {user.email} failed (may not have Chat permissions): {e}"
        )


# ── PO Email Verification ────────────────────────────────────────────────


async def verify_po_sent(plan: BuyPlan, db: Session) -> dict:
    """Scan buyers' sent emails to verify PO was sent. Returns verification results."""
    from ..scheduler import get_valid_token
    from ..utils.graph_client import GraphClient

    results = {}
    updated = False

    for i, item in enumerate(plan.line_items or []):
        po_number = item.get("po_number")
        if not po_number or item.get("po_verified"):
            continue

        # Find the buyer who entered the offer
        entered_by_id = item.get("entered_by_id")
        if not entered_by_id and item.get("offer_id"):
            offer = db.get(Offer, item["offer_id"])
            entered_by_id = offer.entered_by_id if offer else None
        if not entered_by_id:
            continue

        buyer = db.get(User, entered_by_id)
        if not buyer:
            continue

        try:
            token = await get_valid_token(buyer, db)
            if not token:
                results[po_number] = {"verified": False, "reason": "no_token"}
                continue

            gc = GraphClient(token)
            # Search sent items for the PO number
            search_result = await gc.get_json(
                "/me/mailFolders/sentItems/messages",
                params={
                    "$search": f'"{po_number}"',
                    "$top": "5",
                    "$select": "subject,toRecipients,sentDateTime",
                    "$orderby": "sentDateTime desc",
                },
            )
            messages = search_result.get("value", [])
            if messages:
                msg = messages[0]
                recipients = msg.get("toRecipients", [])
                po_recipient = (
                    recipients[0]["emailAddress"]["address"] if recipients else None
                )
                po_sent_at = msg.get("sentDateTime")

                # Update line item
                item["po_verified"] = True
                item["po_recipient"] = po_recipient
                item["po_sent_at"] = po_sent_at
                updated = True

                results[po_number] = {
                    "verified": True,
                    "recipient": po_recipient,
                    "sent_at": po_sent_at,
                    "subject": msg.get("subject", ""),
                }
            else:
                results[po_number] = {"verified": False, "reason": "not_found"}

        except Exception as e:
            log.error(f"PO verification failed for {po_number}: {e}")
            results[po_number] = {"verified": False, "reason": str(e)}

    if updated:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(plan, "line_items")
        # Check if all POs are verified
        all_verified = all(
            item.get("po_verified")
            for item in (plan.line_items or [])
            if item.get("po_number")
        )
        if all_verified and plan.status == "po_entered":
            plan.status = "po_confirmed"
        db.commit()

    return results
