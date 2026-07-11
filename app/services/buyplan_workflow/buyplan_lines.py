"""Buy Plan — Line management: claim, flag/resolve issues, re-source, add/edit/remove.

Split from the former monolithic `buyplan_workflow.py` (P4.3) along the "line
management" seam: the re-source → open-claim-pool pipeline (``resource_line`` /
``claim_line``), buyer issue flagging (``flag_line_issue`` / ``resolve_line_issue``),
the role×status edit gate, and the salesperson/manager line add/edit/remove API
(epic I) — plus its bulk "save all" counterpart (``bulk_edit_buy_plan_lines``) — and
the Sales Order number editor (epic J).

Called by: routers/htmx/buy_plans.py, services/buyplan_service.py, services/buyplan_hub.py
Depends on: buyplan_scoring (assign_buyer, score_offer), buyplan_approval
    (_recalculate_financials, _cancel_open_prepayment_requests_for_plan, _can_halt),
    po_cancellation_service
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import update
from sqlalchemy.orm import Session, joinedload

from ...constants import UserRole
from ...models import Offer, Requirement, User
from ...models.buy_plan import BuyPlan, BuyPlanLine, BuyPlanLineStatus, BuyPlanStatus
from ..buyplan_scoring import assign_buyer, score_offer
from .buyplan_approval import _can_halt, _cancel_open_prepayment_requests_for_plan, _recalculate_financials

# ── Workflow: Re-source (PO cancelled → open claim pool) ─────────────

# A live PO exists only on these line statuses, so only these can be re-sourced.
RESOURCEABLE_LINE_STATUSES = {
    BuyPlanLineStatus.PENDING_VERIFY.value,
    BuyPlanLineStatus.VERIFIED.value,
}


def resource_line(
    plan_id: int,
    line_id: int,
    reason_code: str,
    reason_note: str | None,
    user: User,
    db: Session,
    also_line_ids: list[int] | None = None,
) -> dict:
    """Re-source one (default) or several fallen-down lines back into the open claim
    pool.

    The single fall-down → re-source engine for BOTH triggers (do NOT build a parallel
    one): the SP-3 vendor-cancel (the buyer cancelled a cut PO before delivery) and the
    SP-4 receiving-reject (parts arrived but were rejected at receiving — defective /
    wrong / short). For each target line this:
      1. records an immutable POCancellation (vendor-performance fact),
      2. marks the vendor's offer SOLD + the vendor unavailable for that part,
      3. resets the line into the pool (unassigned, no PO/offer, status RESOURCING),
    then reopens the plan if it had auto-completed/closed (COMPLETED) and refreshes the
    canceled vendors' cancellation metrics. Returns a payload the route hands to the
    urgent-alert fan-out — including ``was_completed`` (True when a COMPLETED plan was
    reopened here, i.e. a backorder emergency) so the fan-out can force the alert.

    Escalation: ``also_line_ids`` re-sources sibling lines on the SAME plan in one action
    (the hybrid scope — default is just ``line_id``).
    """
    from ...constants import LineResourceReason
    from ..po_cancellation_service import (
        mark_offer_sold,
        mark_vendor_unavailable,
        record_po_cancellation,
        refresh_vendor_cancellation_metrics,
    )

    plan = db.get(BuyPlan, plan_id, options=[joinedload(BuyPlan.lines)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")

    # Only ACTIVE (live) or COMPLETED (auto-completed, e.g. an SP-4 receiving-reject after
    # the fact) plans can be re-sourced — COMPLETED is reopened to ACTIVE below. A VERIFIED
    # line can survive on a CANCELLED/HALTED plan (cancel only cascades open lines), but
    # re-sourcing it would dead-end — claim → confirm_po needs an ACTIVE plan, which a
    # cancelled/halted plan can never become here.
    if plan.status not in (
        BuyPlanStatus.ACTIVE.value,
        BuyPlanStatus.COMPLETED.value,
    ):
        raise ValueError(f"Cannot re-source on a {plan.status} plan (must be active or completed)")

    reason_code = LineResourceReason(reason_code).value  # validate the dropdown value

    target_ids = {line_id} | {int(i) for i in (also_line_ids or [])}
    targets = [ln for ln in plan.lines if ln.id in target_ids]
    if not targets:
        raise ValueError(f"No lines {sorted(target_ids)} found in plan {plan_id}")

    resourced: list[dict] = []
    vendor_card_ids: set[int] = set()

    for line in targets:
        if line.status not in RESOURCEABLE_LINE_STATUSES:
            raise ValueError(f"Line {line.id} has no live PO to re-source (status: {line.status})")

        offer = line.offer
        requirement = line.requirement

        # ── Side effects (all NO-COMMIT; the route owns the transaction). Order
        #    matters: record reads line.po_confirmed_at BEFORE we clear it below.
        #    A live-PO line can lose its offer (offer_id is SET NULL on offer delete) —
        #    without an offer there is no vendor to attribute the cancellation to, so we
        #    skip the cancellation/sold/unavailable side effects and still pool the line. ──
        if offer:
            record_po_cancellation(
                db,
                line=line,
                offer=offer,
                requirement=requirement,
                reason_code=reason_code,
                reason_text=reason_note,
                user=user,
            )
            mark_offer_sold(db, offer, user)
            mark_vendor_unavailable(
                db, requirement=requirement, offer=offer, reason_code=reason_code, note=reason_note, user=user
            )
            if offer.vendor_card_id:
                vendor_card_ids.add(offer.vendor_card_id)
        else:
            logger.warning(
                "Re-source line {} has no offer (offer_id NULL) — pooling without a cancellation fact",
                line.id,
            )

        resourced.append(
            {
                "line_id": line.id,
                "offer_id": line.offer_id,
                "vendor_name": offer.vendor_name if offer else None,
                "prior_buyer_id": line.buyer_id,
                "po_number": line.po_number,
                "requirement_id": line.requirement_id,
            }
        )

        # ── Reset the line into the open claim pool. Keep requirement_id / quantity /
        #    unit_sell (re-sourcing the same need at the same sell price). ──
        line.buyer_id = None
        line.assignment_reason = None
        line.offer_id = None
        line.unit_cost = None
        line.margin_pct = None
        line.ai_score = None
        line.po_number = None
        line.estimated_ship_date = None
        line.po_confirmed_at = None
        line.po_verified_by_id = None
        line.po_verified_at = None
        line.po_rejection_note = None
        line.issue_type = None
        line.issue_note = None
        line.last_nudge_at = None
        line.status = BuyPlanLineStatus.RESOURCING.value

    # A re-sourced line means its PO/vendor changed underneath, so that line's pending
    # prepayment (wire) is now stale — void it so a manager can't authorise a wire to a
    # vendor no longer on the deal (finding #2, Task 9 extended). LINE-scoped to exactly the
    # re-sourced lines (line_id + also_line_ids) so a sibling line's legitimate REQUESTED
    # prepayment on the same plan is NOT collateral-cancelled.
    _cancel_open_prepayment_requests_for_plan(
        plan_id, db, "buy plan line re-sourced — prepayment voided", line_ids=sorted(target_ids)
    )

    # A COMPLETED (auto-completed/closed) plan must reopen to ACTIVE so the re-claimed
    # line's PO flow (confirm_po requires an ACTIVE plan) works again. ``was_completed``
    # records that this was a completed-plan BACKORDER (a vendor cancelled AFTER the deal
    # closed) so the caller can escalate the re-source broadcast to a forced EMERGENCY
    # alert — the fact must ride the return value because the plan is ACTIVE again by the
    # time the notification runs.
    was_completed = plan.status == BuyPlanStatus.COMPLETED.value
    if was_completed:
        plan.status = BuyPlanStatus.ACTIVE.value
        plan.completed_at = None
        plan.case_report = None

    # Flush so the new POCancellation rows are visible to the metric refresh
    # (the test session runs autoflush=False).
    db.flush()
    for vcid in vendor_card_ids:
        refresh_vendor_cancellation_metrics(db, vcid)

    db.flush()
    logger.info(
        "Re-sourced {} line(s) on plan {} by {} (reason: {})",
        len(resourced),
        plan_id,
        user.email,
        reason_code,
    )
    return {
        "plan_id": plan_id,
        "actor_id": user.id,
        "reason_code": reason_code,
        "reason_note": reason_note,
        "resourced_lines": resourced,
        "was_completed": was_completed,
    }


def claim_line(plan_id: int, line_id: int, user: User, db: Session) -> BuyPlanLine:
    """First-to-claim wins: take an open-pool (RESOURCING, unassigned) line.

    Atomic guarded UPDATE — succeeds only while the line is still RESOURCING and
    unassigned. Under PostgreSQL READ COMMITTED the second concurrent claimer blocks on
    the row lock, re-evaluates the predicate after the winner commits, matches nothing,
    and gets a clean ValueError (the route maps it to HTTP 409).
    """
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")

    result = db.execute(
        update(BuyPlanLine)
        .where(
            BuyPlanLine.id == line_id,
            BuyPlanLine.buy_plan_id == plan_id,
            BuyPlanLine.status == BuyPlanLineStatus.RESOURCING.value,
            BuyPlanLine.buyer_id.is_(None),
        )
        .values(
            buyer_id=user.id,
            assignment_reason="claimed",
            status=BuyPlanLineStatus.AWAITING_PO.value,
            last_nudge_at=None,
        )
    )
    if result.rowcount == 0:
        raise ValueError("Line was already claimed or is no longer in re-sourcing")

    db.flush()
    line = db.get(BuyPlanLine, line_id)
    db.refresh(line)
    logger.info("Line {} (plan {}) claimed by {}", line_id, plan_id, user.email)
    return line


# ── Workflow: Issue Flagging ─────────────────────────────────────────


def flag_line_issue(
    plan_id: int,
    line_id: int,
    issue_type: str,
    user: User,
    db: Session,
    *,
    note: str | None = None,
) -> BuyPlanLine:
    """Buyer flags an issue on a line (sold out, price change, etc.).

    Line status → issue. Manager/salesperson needs to resolve.
    """
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.ACTIVE.value:
        raise ValueError(f"Plan must be active (current: {plan.status})")

    line = db.get(BuyPlanLine, line_id)
    if not line or line.buy_plan_id != plan_id:
        raise ValueError(f"Line {line_id} not found in plan {plan_id}")

    flaggable = {BuyPlanLineStatus.AWAITING_PO.value, BuyPlanLineStatus.PENDING_VERIFY.value}
    if line.status not in flaggable:
        raise ValueError(f"Cannot flag issue on line with status: {line.status}")

    line.status = BuyPlanLineStatus.ISSUE.value
    line.issue_type = issue_type
    line.issue_note = note
    logger.info("Issue '{}' flagged on line {} (plan {})", issue_type, line_id, plan_id)

    db.flush()
    return line


def resolve_line_issue(plan_id: int, line_id: int, user: User, db: Session) -> BuyPlanLine:
    """Clear a flagged issue on a line, returning it to awaiting_po so the buyer can re-
    cut.

    The counterpart to :func:`flag_line_issue`. Without it an ISSUE line was a dead-end — the
    UI showed only a badge and re-source rejects the ISSUE status, so the only escape was
    halting the whole plan. Flagged lines route to supervisors on the My Queue (the buyer who
    raised the issue can't self-resolve), so this action carries the same supervisor/ops
    authority as :func:`halt_plan`. The PO-confirmation fields are cleared alongside the issue
    so ``awaiting_po`` means what it always means (no confirmed PO); the buyer re-confirms.
    """
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.ACTIVE.value:
        raise ValueError(f"Plan must be active (current: {plan.status})")

    if not _can_halt(user, db):
        raise PermissionError("Only a supervisor or ops member can resolve a flagged issue")

    line = db.get(BuyPlanLine, line_id)
    if not line or line.buy_plan_id != plan_id:
        raise ValueError(f"Line {line_id} not found in plan {plan_id}")
    if line.status != BuyPlanLineStatus.ISSUE.value:
        raise ValueError(f"Line has no issue to resolve (current: {line.status})")

    line.status = BuyPlanLineStatus.AWAITING_PO.value
    line.issue_type = None
    line.issue_note = None
    line.po_number = None
    line.estimated_ship_date = None
    line.po_confirmed_at = None
    line.last_nudge_at = None
    logger.info("Issue resolved on line {} (plan {}) by {}", line_id, plan_id, user.email)

    db.flush()
    return line


# ── Editing: role/status gate + line add/edit/remove (epic I) ─────────

# The buy plan is money-governing, so line edits are gated by BOTH the plan's lifecycle
# status AND the actor's role (the manager's post-approval edit authority IS the control —
# no re-approval is triggered):
#   • draft / pending   → the owner (sales/trader) OR a manager may edit (pre-approval);
#   • active / inbound / halted → MANAGER-only (sales is locked out post-approval);
#   • completed / cancelled     → locked for everyone (terminal).
# Ownership for the pre-approval branch derives through the parent requisition
# (BuyPlan.requisition_id is NOT NULL); the router's get_buyplan_for_user has already
# 404'd a restricted-role non-owner before these run.
_MANAGER_ONLY_EDIT_STATUSES = frozenset(
    {BuyPlanStatus.ACTIVE.value, BuyPlanStatus.INBOUND.value, BuyPlanStatus.HALTED.value}
)
_LOCKED_EDIT_STATUSES = frozenset({BuyPlanStatus.COMPLETED.value, BuyPlanStatus.CANCELLED.value})


def _is_manager_or_admin(user: User) -> bool:
    """True for MANAGER/ADMIN (mirrors dependencies.is_manager_or_admin; inlined to
    avoid a service→dependencies import cycle)."""
    return user.role in (UserRole.MANAGER, UserRole.ADMIN)


def _owns_plan(user: User, plan: BuyPlan) -> bool:
    """True when *user* originated the plan's requisition (the plan owner /
    salesperson)."""
    req = plan.requisition
    return bool(req and req.created_by == user.id)


def can_edit_buy_plan_lines(user: User, plan: BuyPlan) -> bool:
    """Whether *user* may add/remove/edit *plan*'s lines given its lifecycle status.

    See the role×status matrix above. Enforced server-side (never UI-only) by
    ``_ensure_can_edit_lines`` in every mutating endpoint.
    """
    status = plan.status
    if status in _LOCKED_EDIT_STATUSES:
        return False
    if status in _MANAGER_ONLY_EDIT_STATUSES:
        return _is_manager_or_admin(user)
    # draft / pending — pre-approval: the plan owner (sales/trader) or a manager.
    return _is_manager_or_admin(user) or _owns_plan(user, plan)


def _ensure_can_edit_lines(user: User, plan: BuyPlan) -> None:
    """Raise PermissionError (→ 403) unless *user* may edit *plan*'s lines now."""
    if not can_edit_buy_plan_lines(user, plan):
        raise PermissionError("You cannot edit this buy plan's lines in its current status.")


def _line_margin_pct(unit_sell: float | None, unit_cost: float | None) -> float | None:
    """Per-line margin % from sell/cost (None when sell is missing/zero)."""
    if unit_sell and unit_cost and unit_sell > 0:
        return round(((unit_sell - unit_cost) / unit_sell) * 100, 2)
    return None


def _has_cut_po(line: BuyPlanLine) -> bool:
    """True once a line has left AWAITING_PO (a PO is cut / verified / flagged /
    cancelled).

    Vendor/qty/removal edits on such a line would corrupt live purchasing state, so they
    are refused (the header sell price can still be corrected — it does not touch the
    PO).
    """
    return line.po_confirmed_at is not None or line.status != BuyPlanLineStatus.AWAITING_PO.value


def _ensure_offer_on_requisition(offer: Offer, requisition_id: int | None) -> None:
    """Reject an offer that isn't scoped to the plan's requisition.

    Mirrors the detail-render's vendor-picker filter (``Offer.requisition_id ==
    bp.requisition_id`` in ``buy_plan_detail_partial``, app/routers/htmx/buy_plans.py)
    so the server enforces the exact same universe the picker/add-form ever shows — an
    offer from a different requisition (or an unsolicited inbound offer with no
    requisition at all) is refused, not just hidden from the UI.
    """
    if offer.requisition_id != requisition_id:
        raise ValueError("That offer is not on this plan's requisition.")


def _require_int_quantity(value: object) -> int:
    """Coerce a JSON quantity value to a whole number, rejecting a fractional value
    (e.g. ``3.5``) instead of silently truncating it via ``int()``."""
    if isinstance(value, bool):
        raise ValueError("Quantity must be a whole number.")
    if isinstance(value, int):
        return value
    try:
        as_float = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ValueError("Quantity must be a whole number.") from e
    if not as_float.is_integer():
        raise ValueError("Quantity must be a whole number.")
    return int(as_float)


def add_buy_plan_line(
    plan_id: int,
    requirement_id: int,
    offer_id: int,
    quantity: int,
    user: User,
    db: Session,
    *,
    unit_sell: float | None = None,
) -> BuyPlan:
    """Add a new line (vendor offer + qty + sell) and recompute the header rollups.

    Gated by :func:`can_edit_buy_plan_lines`. The requirement must belong to the plan's
    requisition and the offer must exist AND belong to the same requisition. Caller
    commits.
    """
    plan = db.get(BuyPlan, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    _ensure_can_edit_lines(user, plan)

    if not quantity or quantity <= 0:
        raise ValueError("Quantity must be a positive whole number.")

    requirement = db.get(Requirement, requirement_id)
    if not requirement or requirement.requisition_id != plan.requisition_id:
        raise ValueError("That part is not on this plan's requisition.")

    offer = db.get(Offer, offer_id)
    if not offer:
        raise ValueError(f"Offer {offer_id} not found")
    _ensure_offer_on_requisition(offer, plan.requisition_id)

    unit_cost = float(offer.unit_price) if offer.unit_price else None
    resolved_sell = (
        float(unit_sell)
        if unit_sell is not None
        else (float(requirement.target_price) if requirement.target_price else None)
    )
    buyer, reason = assign_buyer(offer, offer.vendor_card, db)

    plan.lines.append(
        BuyPlanLine(
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=quantity,
            unit_cost=unit_cost,
            unit_sell=resolved_sell,
            margin_pct=_line_margin_pct(resolved_sell, unit_cost),
            ai_score=score_offer(offer, requirement, offer.vendor_card),
            buyer_id=buyer.id if buyer else None,
            assignment_reason=reason,
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
    )
    _recalculate_financials(plan)
    db.flush()
    logger.info("Buy plan {} line added by {} (req {}, offer {})", plan_id, user.email, requirement_id, offer_id)
    return plan


def edit_buy_plan_line(
    plan_id: int,
    line_id: int,
    user: User,
    db: Session,
    *,
    quantity: int | None = None,
    unit_sell: float | None = None,
    offer_id: int | None = None,
) -> BuyPlan:
    """Edit a line's qty / sell price / vendor(offer) and recompute the header rollups.

    Gated by :func:`can_edit_buy_plan_lines`. Vendor and qty changes are refused once a PO
    is cut on the line (would corrupt live purchasing); the sell price stays editable. Caller
    commits.
    """
    plan = db.get(BuyPlan, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    _ensure_can_edit_lines(user, plan)

    line = next((ln for ln in plan.lines if ln.id == line_id), None)
    if not line:
        raise ValueError(f"Line {line_id} not found in plan {plan_id}")

    if offer_id is not None:
        if _has_cut_po(line):
            raise ValueError("Cannot change the vendor after a PO is cut on this line.")
        offer = db.get(Offer, offer_id)
        if not offer:
            raise ValueError(f"Offer {offer_id} not found")
        _ensure_offer_on_requisition(offer, plan.requisition_id)
        line.offer_id = offer.id
        line.unit_cost = float(offer.unit_price) if offer.unit_price else None
        buyer, reason = assign_buyer(offer, offer.vendor_card, db)
        line.buyer_id = buyer.id if buyer else None
        line.assignment_reason = reason

    if quantity is not None:
        if _has_cut_po(line):
            raise ValueError("Cannot change the quantity after a PO is cut on this line.")
        if quantity <= 0:
            raise ValueError("Quantity must be a positive whole number.")
        line.quantity = quantity

    if unit_sell is not None:
        line.unit_sell = float(unit_sell)

    line.margin_pct = _line_margin_pct(
        float(line.unit_sell) if line.unit_sell is not None else None,
        float(line.unit_cost) if line.unit_cost is not None else None,
    )
    _recalculate_financials(plan)
    db.flush()
    logger.info("Buy plan {} line {} edited by {}", plan_id, line_id, user.email)
    return plan


def bulk_edit_buy_plan_lines(
    plan_id: int,
    lines_payload: list[dict],
    user: User,
    db: Session,
    *,
    known_line_ids: list[int] | None = None,
) -> BuyPlan:
    """Save an entire plan's lines in one shot — edits, adds, and removal-by-omission.

    The "save all" counterpart to :func:`add_buy_plan_line` / :func:`edit_buy_plan_line` /
    :func:`remove_buy_plan_line`, reusing their exact per-field rules rather than
    duplicating them:
      - an entry with ``line_id`` edits that existing line. Vendor/qty changes are
        refused once :func:`_has_cut_po` is true — UNLESS the submitted value equals the
        line's current value, which is always a no-op (never trips the guard); this
        keeps a resend of an untouched row from 400ing the whole save just because a PO
        was cut on it between form-load and save. An actual offer change re-derives
        unit_cost and re-runs :func:`assign_buyer`; the new offer must belong to the
        plan's requisition (:func:`_ensure_offer_on_requisition`). Sell price uses
        key-presence semantics: ``"unit_sell"`` absent from the entry leaves it
        unchanged; present with JSON ``null`` clears it; present with a number sets it.
      - an entry without ``line_id`` adds a new line (requirement must belong to the
        plan's requisition; offer must exist AND belong to the same requisition; qty
        must be a positive whole number — a fractional qty like ``3.5`` is rejected, not
        truncated), same as :func:`add_buy_plan_line`;
      - any existing, non-PO-cut line whose id does NOT appear in the payload is
        removed (same PO-cut guard as :func:`remove_buy_plan_line`, applied by omission
        instead of an explicit call) — a PO-cut line left out of the payload is simply
        left untouched, never implicitly removed. When *known_line_ids* is given,
        removal-by-omission is further scoped to ids IN that set: a line added by
        someone else after the client's form loaded (present on the plan, but never in
        *known_line_ids*) is left untouched instead of being silently deleted.
        *known_line_ids* omitted (``None``) falls back to the legacy behavior above (the
        route always sends it; this is a backward-compat contract, not a UI choice).

    Gated by :func:`can_edit_buy_plan_lines`. Caller commits; a mid-loop ValueError
    leaves nothing committed (the router never calls db.commit() after an exception).
    """
    plan = db.get(BuyPlan, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    _ensure_can_edit_lines(user, plan)

    existing_by_id = {ln.id: ln for ln in plan.lines}
    seen_ids: set[int] = set()
    touched = 0

    for entry in lines_payload:
        if not isinstance(entry, dict):
            raise ValueError("Each line in the payload must be a JSON object.")
        try:
            raw_line_id = entry.get("line_id")
            if raw_line_id is not None:
                line_id = int(raw_line_id)
                line = existing_by_id.get(line_id)
                if not line:
                    raise ValueError(f"Line {line_id} not found in plan {plan_id}")
                seen_ids.add(line_id)

                offer_id = entry.get("offer_id")
                if offer_id is not None:
                    offer_id_int = int(offer_id)
                    if offer_id_int != line.offer_id:
                        if _has_cut_po(line):
                            raise ValueError("Cannot change the vendor after a PO is cut on this line.")
                        offer = db.get(Offer, offer_id_int)
                        if not offer:
                            raise ValueError(f"Offer {offer_id} not found")
                        _ensure_offer_on_requisition(offer, plan.requisition_id)
                        line.offer_id = offer.id
                        line.unit_cost = float(offer.unit_price) if offer.unit_price else None
                        buyer, reason = assign_buyer(offer, offer.vendor_card, db)
                        line.buyer_id = buyer.id if buyer else None
                        line.assignment_reason = reason

                raw_quantity = entry.get("quantity")
                if raw_quantity is not None:
                    quantity = _require_int_quantity(raw_quantity)
                    if quantity != line.quantity:
                        if _has_cut_po(line):
                            raise ValueError("Cannot change the quantity after a PO is cut on this line.")
                        if quantity <= 0:
                            raise ValueError("Quantity must be a positive whole number.")
                        line.quantity = quantity

                # Key-presence semantics: absent -> unchanged; present + null -> clear;
                # present + number -> set. (Unlike offer/quantity, sell is never gated
                # by _has_cut_po — it never touches the PO.)
                if "unit_sell" in entry:
                    raw_sell = entry.get("unit_sell")
                    line.unit_sell = float(raw_sell) if raw_sell is not None else None

                line.margin_pct = _line_margin_pct(
                    float(line.unit_sell) if line.unit_sell is not None else None,
                    float(line.unit_cost) if line.unit_cost is not None else None,
                )
            else:
                requirement_id = entry.get("requirement_id")
                offer_id = entry.get("offer_id")
                raw_quantity = entry.get("quantity")
                quantity = _require_int_quantity(raw_quantity) if raw_quantity is not None else 0
                if quantity <= 0:
                    raise ValueError("Quantity must be a positive whole number.")

                requirement = db.get(Requirement, int(requirement_id)) if requirement_id is not None else None
                if not requirement or requirement.requisition_id != plan.requisition_id:
                    raise ValueError("That part is not on this plan's requisition.")

                offer = db.get(Offer, int(offer_id)) if offer_id is not None else None
                if not offer:
                    raise ValueError(f"Offer {offer_id} not found")
                _ensure_offer_on_requisition(offer, plan.requisition_id)

                unit_sell = entry.get("unit_sell")
                unit_cost = float(offer.unit_price) if offer.unit_price else None
                resolved_sell = (
                    float(unit_sell)
                    if unit_sell is not None
                    else (float(requirement.target_price) if requirement.target_price else None)
                )
                buyer, reason = assign_buyer(offer, offer.vendor_card, db)
                new_line = BuyPlanLine(
                    requirement_id=requirement.id,
                    offer_id=offer.id,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    unit_sell=resolved_sell,
                    margin_pct=_line_margin_pct(resolved_sell, unit_cost),
                    ai_score=score_offer(offer, requirement, offer.vendor_card),
                    buyer_id=buyer.id if buyer else None,
                    assignment_reason=reason,
                    status=BuyPlanLineStatus.AWAITING_PO.value,
                )
                plan.lines.append(new_line)
        except (TypeError, KeyError) as e:
            raise ValueError(f"Malformed line payload: {e}") from e
        touched += 1

    # Removal by omission: an existing, non-PO-cut line not referenced by line_id in the
    # payload is dropped — UNLESS known_line_ids was given and the line's id isn't in
    # it, meaning the client never saw this line (added concurrently by someone else
    # after the form loaded) and it must be left alone. A PO-cut line omitted from the
    # payload is always left alone regardless — it can only leave the plan via the
    # explicit re-source / PO-cancellation flow.
    for line in list(existing_by_id.values()):
        if line.id in seen_ids or _has_cut_po(line):
            continue
        if known_line_ids is not None and line.id not in known_line_ids:
            continue
        plan.lines.remove(line)

    _recalculate_financials(plan)
    db.flush()
    logger.info(
        "Buy plan {} bulk-saved by {} ({} line(s) touched, {} total)",
        plan_id,
        user.email,
        touched,
        len(plan.lines),
    )
    return plan


def remove_buy_plan_line(plan_id: int, line_id: int, user: User, db: Session) -> BuyPlan:
    """Remove a line and recompute the header rollups.

    Gated by :func:`can_edit_buy_plan_lines`. A line with a cut PO cannot be removed (it must
    be re-sourced / cancelled through the PO lifecycle). Caller commits.
    """
    plan = db.get(BuyPlan, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    _ensure_can_edit_lines(user, plan)

    line = next((ln for ln in plan.lines if ln.id == line_id), None)
    if not line:
        raise ValueError(f"Line {line_id} not found in plan {plan_id}")
    if _has_cut_po(line):
        raise ValueError("Cannot remove a line once a PO is cut on it.")

    plan.lines.remove(line)
    _recalculate_financials(plan)
    db.flush()
    logger.info("Buy plan {} line {} removed by {}", plan_id, line_id, user.email)
    return plan


# ── Editing: Sales Order number (epic J) ──────────────────────────────


def set_sales_order_number(plan_id: int, sales_order_number: str | None, user: User, db: Session) -> BuyPlan:
    """Set/clear the active Sales Order number on a non-terminal plan.

    The salesperson (or a manager) enters the real order number once the deal is placed.
    Only editable while the plan is non-terminal (completed/cancelled are locked). The
    owner/manager gate is enforced by the router; caller commits.
    """
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status in _LOCKED_EDIT_STATUSES:
        raise ValueError(f"Cannot edit the Sales Order number on a {plan.status} plan.")

    plan.sales_order_number = (sales_order_number or "").strip() or None
    plan.updated_at = datetime.now(UTC)
    db.flush()
    logger.info("Buy plan {} SO number set to {!r} by {}", plan_id, plan.sales_order_number, user.email)
    return plan
