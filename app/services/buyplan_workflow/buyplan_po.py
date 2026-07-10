"""Buy Plan — PO confirm/verify: buyer PO confirmation and approver PO sign-off.

Split from the former monolithic `buyplan_workflow.py` (P4.3) along the "PO
confirm/verify" seam. ``verify_po``'s completion check is a lazy (function-local)
import of `buyplan_approval.check_completion` — a top-level import would cycle
against `buyplan_approval`'s top-level import of ``_line_amount`` from this module.

Called by: routers/htmx/buy_plans.py, jobs/inventory_jobs.py, routers/prepayments.py
Depends on: dependencies (can_approve_purchase_orders), buyplan_approval (lazy),
    activity_service, utils/graph_client, utils/token_manager
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.orm import Session

from ...models import User
from ...models.buy_plan import BuyPlan, BuyPlanLine, BuyPlanLineStatus, BuyPlanStatus

# ── Workflow: PO Execution ───────────────────────────────────────────


def confirm_po(
    plan_id: int,
    line_id: int,
    po_number: str,
    estimated_ship_date: datetime,
    user: User,
    db: Session,
) -> BuyPlanLine:
    """Buyer confirms PO was cut for a line in Acctivate.

    Line status: awaiting_po → pending_verify.
    """
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.ACTIVE.value:
        raise ValueError(f"Plan must be active (current: {plan.status})")

    line = db.get(BuyPlanLine, line_id)
    if not line or line.buy_plan_id != plan_id:
        raise ValueError(f"Line {line_id} not found in plan {plan_id}")
    if line.status != BuyPlanLineStatus.AWAITING_PO.value:
        raise ValueError(f"Line must be awaiting PO (current: {line.status})")

    line.po_number = po_number
    line.estimated_ship_date = estimated_ship_date
    line.po_confirmed_at = datetime.now(UTC)
    line.status = BuyPlanLineStatus.PENDING_VERIFY.value
    logger.info("PO {} confirmed for line {} (plan {})", po_number, line_id, plan_id)

    db.flush()
    return line


def _line_amount(line: BuyPlanLine) -> float:
    """Dollar amount of one line's PO (``unit_cost * quantity``).

    Mirrors ``_recalculate_financials``'s per-line cost math — the single grain for the
    per-PO dollar-limit check (``verify_po``, ``can_verify_po_line``) and the per-line
    stall detectors (``plan_needs_approver_reason``, hub ``_query_stuck_no_approver_plans``).
    """
    return float(line.unit_cost or 0) * (line.quantity or 0)


def _log_po_line_activity(
    plan: BuyPlan, line: BuyPlanLine, action: str, user: User, note: str | None, db: Session
) -> None:
    """Record an ActivityLog row for a per-line PO verify/reject decision (audit trail).

    Mirrors ``_log_approval_activity`` — a durable, timeline-visible record of who signed
    off (or sent back) which PO, replacing the log-line-only trail.
    """
    from ...constants import ActivityType
    from ..activity_service import log_activity

    verb = "verified" if action == "approve" else "rejected"
    activity_type = ActivityType.PO_LINE_VERIFIED if action == "approve" else ActivityType.PO_LINE_REJECTED
    po_label = f"PO {line.po_number}" if line.po_number else f"PO (line {line.id})"
    description = f"{po_label} on buy plan #{plan.id} {verb} by {user.name or user.email}"
    if note:
        description = f"{description}: {note}"

    # BuyPlan.requisition_id is NOT NULL, so log_activity resolves the company from the
    # requisition — the row lands on the customer timeline (same as _log_approval_activity).
    log_activity(
        db,
        activity_type=activity_type,
        user_id=user.id,
        buy_plan_id=plan.id,
        requisition_id=plan.requisition_id,
        description=description,
    )


def verify_po(
    plan_id: int,
    line_id: int,
    action: str,
    user: User,
    db: Session,
    *,
    rejection_note: str | None = None,
) -> BuyPlanLine:
    """A purchase-order approver verifies a PO was properly entered.

    Approve → line verified. Reject → back to awaiting_po. After approval, checks if all
    lines are done → auto-complete. This per-line decision IS the PO approval (Phase 3
    retired the redundant deal-level PURCHASE_ORDER engine gate).

    Phase D: the gate moved off ops verification-group membership onto the per-user
    ``can_approve_purchase_orders`` right. Phase 3 additionally enforces the approver's
    admin-configured ``purchase_order_approval_limit`` against THIS line's dollar amount
    (NULL = unlimited) — the same check ``can_verify_po_line`` uses to hide the buttons.
    """
    from ...dependencies import can_approve_purchase_orders

    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")

    line = db.get(BuyPlanLine, line_id)
    if not line or line.buy_plan_id != plan_id:
        raise ValueError(f"Line {line_id} not found in plan {plan_id}")
    if line.status != BuyPlanLineStatus.PENDING_VERIFY.value:
        raise ValueError(f"Line must be pending verification (current: {line.status})")

    if not can_approve_purchase_orders(user):
        raise PermissionError("Purchase-order approval right required to verify a PO")
    limit = getattr(user, "purchase_order_approval_limit", None)
    if limit is not None and _line_amount(line) > limit:
        raise PermissionError(
            f"PO amount ${_line_amount(line):,.2f} exceeds your purchase-order approval limit (${limit:,.2f})"
        )

    now = datetime.now(UTC)
    if action == "approve":
        line.status = BuyPlanLineStatus.VERIFIED.value
        line.po_verified_by_id = user.id
        line.po_verified_at = now
        logger.info("PO verified for line {} (plan {})", line_id, plan_id)
        _log_po_line_activity(plan, line, action, user, None, db)
        # Lazy import: avoids a top-level cycle with buyplan_approval (which imports
        # ``_line_amount`` from this module at module load time).
        from .buyplan_approval import check_completion

        check_completion(plan_id, db)
    elif action == "reject":
        # Log BEFORE the reset below clears line.po_number (the audit row names the PO).
        _log_po_line_activity(plan, line, action, user, rejection_note, db)
        line.status = BuyPlanLineStatus.AWAITING_PO.value
        line.po_rejection_note = rejection_note
        line.po_number = None
        line.estimated_ship_date = None
        line.po_confirmed_at = None
        # Reset the nudge clock: the line is actionable again, so the buyer is re-nudged
        # to re-issue the PO without waiting out a stale (ops-stamped) nudge window.
        line.last_nudge_at = None
        logger.info("PO rejected for line {}: {}", line_id, rejection_note)
    else:
        raise ValueError(f"Invalid PO verification action: {action}")

    db.flush()
    return line


# ── Workflow: PO Verification Scanning ─────────────────────────────


async def verify_po_sent(plan: "BuyPlan", db: "Session") -> list[dict]:
    """Scan buyer's Outlook sent folder for PO emails matching each line (detection
    only).

    For each line with a po_number, searches Graph API for emails containing that PO
    number and reports whether one was found. This is a NON-AUTHORITATIVE signal: it does
    NOT verify the line or complete the plan — verification is gated behind verify_po's
    ``can_approve_purchase_orders`` right (Phase D). Each result carries
    ``awaiting_approver_verification`` so callers can flag PENDING_VERIFY lines whose PO
    email was detected for an approver to sign off.
    """
    from ...utils.graph_client import GraphClient
    from ...utils.token_manager import get_valid_token

    results = []
    for line in plan.lines:
        if not line.po_number:
            results.append({"line_id": line.id, "po_number": None, "found": False, "skipped": True})
            continue

        # Get buyer's Graph token
        if not line.buyer_id:
            results.append(
                {"line_id": line.id, "po_number": line.po_number, "found": False, "skipped": True, "reason": "no_buyer"}
            )
            continue

        try:
            buyer = db.get(User, line.buyer_id)
            if not buyer:
                results.append(
                    {
                        "line_id": line.id,
                        "po_number": line.po_number,
                        "found": False,
                        "skipped": True,
                        "reason": "buyer_not_found",
                    }
                )
                continue

            token = await get_valid_token(buyer, db)
            if not token:
                results.append(
                    {
                        "line_id": line.id,
                        "po_number": line.po_number,
                        "found": False,
                        "skipped": True,
                        "reason": "no_token",
                    }
                )
                continue

            client = GraphClient(token)
            # Search sent folder for PO number
            messages = await client.search_sent_messages(
                query=line.po_number,
                user_id=str(buyer.azure_id),
            )

            found = len(messages) > 0
            # DETECTION ONLY — finding the PO email in the buyer's sent folder is a signal,
            # NOT verification. Flipping the line to VERIFIED here bypassed the Phase-D
            # purchase-order-approver gate (can_approve_purchase_orders) that the interactive
            # verify_po enforces, and left po_verified_by_id NULL — letting a buyer who merely
            # emailed a PO complete the deal with no approver signing off. Line verification
            # must go through verify_po; here we only flag lines awaiting that approval.
            awaiting = found and line.status == BuyPlanLineStatus.PENDING_VERIFY.value

            results.append(
                {
                    "line_id": line.id,
                    "po_number": line.po_number,
                    "found": found,
                    "message_count": len(messages),
                    "awaiting_approver_verification": awaiting,
                }
            )
        except Exception as e:
            logger.error("PO verification failed for line {}: {}", line.id, e)
            results.append({"line_id": line.id, "po_number": line.po_number, "found": False, "error": str(e)})

    # No completion side-effect: this scan never verifies a line, so it cannot drive
    # completion. Verified lines complete through verify_po's gated (approver) path.
    # NOTE: flush (not commit) — the caller (PO-verify job) owns the transaction.
    db.flush()
    return results
