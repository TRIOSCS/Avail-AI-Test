"""Buy Plan V1 — PO email verification and auto-complete for stock sales.

Called by: routers/crm/buy_plans.py, jobs/inventory_jobs.py (via buyplan_service façade)
Depends on: utils/graph_client, models, scheduler
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..models import BuyPlan, Offer, User


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
                },
            )
            messages = search_result.get("value", [])
            if messages:
                msg = messages[0]
                recipients = msg.get("toRecipients", [])
                po_recipient = recipients[0]["emailAddress"]["address"] if recipients else None
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
            logger.error(f"PO verification failed for {po_number}: {e}")
            results[po_number] = {"verified": False, "reason": str(e)}

    if updated:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(plan, "line_items")
        # Check if all POs are verified
        all_verified = all(item.get("po_verified") for item in (plan.line_items or []) if item.get("po_number"))
        if all_verified and plan.status == "po_entered":
            plan.status = "po_confirmed"
        db.commit()

    return results


def auto_complete_stock_sales(db: Session) -> int:
    """Complete stock sale plans stuck in 'approved' for 1+ hours (safety net).

    Returns the number of plans auto-completed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    stuck = (
        db.query(BuyPlan)
        .filter(
            BuyPlan.is_stock_sale == True,  # noqa: E712
            BuyPlan.status == "approved",
            BuyPlan.approved_at < cutoff,
        )
        .all()
    )

    completed = 0
    for plan in stuck:
        plan.status = "complete"
        plan.completed_at = datetime.now(timezone.utc)
        # completed_by_id stays None (auto-completed by system)
        logger.info(f"Auto-completed stuck stock sale plan #{plan.id}")
        completed += 1

    if completed:
        db.commit()
    return completed
