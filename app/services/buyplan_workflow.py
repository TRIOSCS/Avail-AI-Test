"""Buy Plan — Workflow: submit, approve, verify, complete, intelligence.

Phase 4: Approval + Execution — submit, approve, verify SO/PO, flag issues,
         auto-complete, favoritism detection, case reports.

Called by: routers/htmx_views.py
Depends on: buyplan_scoring, buyplan_builder, models, config
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..models import (
    Offer,
    Quote,
    Requirement,
    User,
)
from ..models.buy_plan import (
    BuyPlan,
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
    VerificationGroupMember,
)
from .buyplan_scoring import assign_buyer, score_offer

# ── Workflow: Submit ─────────────────────────────────────────────────


def submit_buy_plan(
    plan_id: int,
    sales_order_number: str,
    user: User,
    db: Session,
    *,
    customer_po_number: str | None = None,
    line_edits: list[dict] | None = None,
    salesperson_notes: str | None = None,
) -> BuyPlan:
    """Submit a draft buy plan with SO# and optional line edits.

    Flow: draft → pending (needs manager) OR draft → active (auto-approved).
    Auto-approve when total cost < threshold AND no critical AI flags.
    """
    plan = db.get(BuyPlan, plan_id, options=[joinedload(BuyPlan.lines)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.DRAFT.value:
        raise ValueError(f"Can only submit draft plans (current: {plan.status})")

    plan.sales_order_number = sales_order_number
    plan.customer_po_number = customer_po_number
    plan.submitted_by_id = user.id
    plan.submitted_at = datetime.now(timezone.utc)
    plan.salesperson_notes = salesperson_notes
    # Clear any prior approval decision (a previously-rejected plan re-enters the queue
    # clean — no stale approved_at/approval_notes carrying the old rejection forward).
    plan.approved_by_id = None
    plan.approved_at = None
    plan.approval_notes = None

    if line_edits:
        _apply_line_edits(plan, line_edits, db)

    plan.is_stock_sale = _is_stock_sale(plan, db)

    # Auto-approve decision
    if _should_auto_approve(plan):
        plan.status = BuyPlanStatus.ACTIVE.value
        plan.auto_approved = True
        plan.approved_at = datetime.now(timezone.utc)
        logger.info("Buy plan {} auto-approved (cost={:.2f})", plan_id, float(plan.total_cost or 0))
        _generate_buyer_tasks(plan, db)
    else:
        plan.status = BuyPlanStatus.PENDING.value
        logger.info("Buy plan {} pending approval (cost={:.2f})", plan_id, float(plan.total_cost or 0))
        # Open the engine gate: route a BUY_PLAN ApprovalRequest to can_approve_buy_plans
        # holders (cancels any stale open request first — RISK 2).
        _open_engine_request_for_plan(plan, user, db)

    db.flush()
    return plan


# ── Workflow: Approval ───────────────────────────────────────────────


def approve_buy_plan(
    plan_id: int,
    action: str,
    user: User,
    db: Session,
    *,
    line_overrides: list[dict] | None = None,
    notes: str | None = None,
) -> BuyPlan:
    """Manager approves or rejects a pending buy plan.

    Approve → active (lines go to buyers). Reject → draft (back to salesperson). Line
    overrides let manager swap vendors on specific lines.
    """
    plan = db.get(BuyPlan, plan_id, options=[joinedload(BuyPlan.lines)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.PENDING.value:
        raise ValueError(f"Can only approve/reject pending plans (current: {plan.status})")

    # Single source of truth for the approval right: the per-user can_approve_buy_plans
    # column (admin-toggled, not role-derived). This MUST match the predicate that hides
    # the UI and the require_buyplan_approver dependency that gates the POST.
    from ..dependencies import can_approve_buy_plans

    if not can_approve_buy_plans(user):
        raise PermissionError("Buy-plan approval right required to approve/reject")

    if action == "approve":
        _run_approve_side_effects(plan, user, db, line_overrides=line_overrides, notes=notes)
    elif action == "reject":
        reason = (notes or "").strip()
        if not reason:
            raise ValueError("A rejection reason is required")
        _run_reject_side_effects(plan, user, db, reason=reason)
    else:
        raise ValueError(f"Invalid action: {action}")

    db.flush()
    return plan


def _run_approve_side_effects(
    plan: BuyPlan,
    user: User,
    db: Session,
    *,
    line_overrides: list[dict] | None = None,
    notes: str | None = None,
) -> None:
    """Apply the on-approve side effects to *plan* (status→ACTIVE + buyer tasks).

    The single arbitration point for a buy-plan approval's effects, called by BOTH the
    legacy ``approve_buy_plan`` path and the approvals-engine ``decide()`` dispatch so the
    two paths can never drift. Optional manager ``line_overrides`` swap vendors/quantities
    before the plan is activated. Stamps approver/decision metadata, generates the buyer
    'Cut PO' tasks, and writes the audit ActivityLog row. The caller owns the flush/commit.

    State guard FIRST: only a PENDING plan may be approved. This is the single point that
    protects BOTH approval paths — if an approver decides a STALE engine request whose plan
    has since left PENDING (e.g. it was cancelled or halted out from under the queue), this
    raises cleanly so the router turns it into a 400 / idempotent no-op instead of silently
    resurrecting the cancelled plan to ACTIVE. ``approve_buy_plan`` keeps its own pre-check
    (defense in depth); the engine ``decide()`` dispatch relies on this one.
    """
    if plan.status != BuyPlanStatus.PENDING.value:
        raise ValueError(f"Can only approve a pending plan (current: {plan.status})")
    now = datetime.now(timezone.utc)
    if line_overrides:
        _apply_line_overrides(plan, line_overrides, db)
    plan.status = BuyPlanStatus.ACTIVE.value
    plan.approved_by_id = user.id
    plan.approved_at = now
    plan.approval_notes = notes
    logger.info("Buy plan {} approved by {}", plan.id, user.email)
    _generate_buyer_tasks(plan, db)
    _log_approval_activity(plan, "approve", user, notes, db)


def _run_reject_side_effects(plan: BuyPlan, user: User, db: Session, *, reason: str) -> None:
    """Apply the on-reject side effects to *plan* (status→DRAFT, back to salesperson).

    Counterpart to ``_run_approve_side_effects``; shared by the legacy path and the engine
    ``decide()`` dispatch. Stamps the rejecting user + reason and writes the audit
    ActivityLog row. The caller owns the flush/commit.

    State guard FIRST (same rationale as ``_run_approve_side_effects``): only a PENDING plan
    may be rejected, so deciding a stale request whose plan already left PENDING raises a
    clean ValueError (→ router 400) rather than dragging a cancelled/halted plan back to
    DRAFT.
    """
    if plan.status != BuyPlanStatus.PENDING.value:
        raise ValueError(f"Can only reject a pending plan (current: {plan.status})")
    plan.status = BuyPlanStatus.DRAFT.value
    plan.approved_by_id = user.id
    plan.approved_at = datetime.now(timezone.utc)
    plan.approval_notes = reason
    logger.info("Buy plan {} rejected by {}: {}", plan.id, user.email, reason)
    _log_approval_activity(plan, "reject", user, reason, db)


def _cancel_open_engine_requests_for_plan(plan: BuyPlan, user: User, db: Session) -> int:
    """Cancel every open (REQUESTED) BUY_PLAN ApprovalRequest for *plan* via the engine.

    The single point that closes a plan's engine gate when the plan leaves PENDING — called
    by ``_open_engine_request_for_plan`` (before opening a fresh request, RISK 2: never two
    live REQUESTED rows) AND by the non-decide transitions that take a plan out of PENDING
    (``cancel_buy_plan``, ``verify_so`` HALT). Cancelling the open request there means no
    REQUESTED row is orphaned in the approvals queue/badge for a plan that no longer exists
    to approve — and, crucially, closes the resurrection vector (an approver can no longer
    pick a stale request out of the queue and re-activate a cancelled plan).

    Authz: each request is cancelled on behalf of its OWN ``requested_by`` (falling back to
    ``owner``), the user who originally submitted the plan — so the engine ``cancel`` authz
    (requester/owner OR manager/admin) is satisfied for EVERY transition caller, regardless
    of whether the user driving the transition is the submitter, a manager/admin, or an
    ops-group member who is neither (the verify_so HALT case). The plan-level audit of who
    cancelled/halted the plan is already captured on the plan + its activity log; this
    cancel is a system-driven consequence of the plan leaving PENDING, not a separate
    user-initiated cancel of someone else's request. ``user`` is used only as a final
    fallback actor when a request somehow carries no requester/owner.

    Returns the number of requests cancelled. Lazy imports avoid the circular import (the
    approvals service imports buyplan_workflow for the decide() dispatch).
    """
    from ..constants import (
        ApprovalRequestStatus,
        ApprovalSubjectType,
    )
    from ..models.approvals import ApprovalRequest
    from .approvals.events import cancel as svc_cancel

    open_requests = (
        db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
                ApprovalRequest.subject_id == plan.id,
                ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            )
        )
        .scalars()
        .all()
    )
    cancelled = 0
    for ar in open_requests:
        actor = ar.requested_by or ar.owner or user
        svc_cancel(db, ar.id, actor=actor)
        cancelled += 1
    return cancelled


def _open_engine_request_for_plan(plan: BuyPlan, user: User, db: Session) -> None:
    """Open a BUY_PLAN ApprovalRequest for *plan*, cancelling any stale open one first.

    Called when a plan enters PENDING (submit / resubmit). RISK 2 (double request): a
    resubmit must never leave two REQUESTED rows racing for the same plan, so we cancel
    every existing open (REQUESTED) request for this plan via
    ``_cancel_open_engine_requests_for_plan`` BEFORE creating the fresh one — leaving exactly
    one open request. If no approver holds ``can_approve_buy_plans`` the engine raises
    NoEligibleApproverError; we log a WARNING and leave the plan PENDING with no orphan
    engine state (the create_request flush is the only write, and it is rolled into the
    caller's transaction — a failed route leaves nothing half-built).

    Lazy imports: the approvals service imports buyplan_workflow (decide() dispatch), so a
    top-level import here would be circular.
    """
    from ..constants import ApprovalGateType
    from .approvals.routing import NoEligibleApproverError
    from .approvals.service import create_request

    _cancel_open_engine_requests_for_plan(plan, user, db)

    try:
        create_request(
            db,
            gate_type=ApprovalGateType.BUY_PLAN,
            amount=plan.total_cost,
            subject=plan,
            requested_by=user,
            owner=user,
        )
    except NoEligibleApproverError:
        logger.warning("Buy plan {} pending but no BUY_PLAN approver configured", plan.id)


def _log_approval_activity(plan: BuyPlan, action: str, user: User, notes: str | None, db: Session) -> None:
    """Record an ActivityLog row for an approve/reject decision (audit trail)."""
    from ..constants import ActivityType
    from .activity_service import log_activity

    verb = "approved" if action == "approve" else "rejected"
    activity_type = ActivityType.BUYPLAN_APPROVED if action == "approve" else ActivityType.BUYPLAN_REJECTED
    description = f"Buy plan #{plan.id} {verb} by {user.name or user.email}"
    if notes:
        description = f"{description}: {notes}"

    # BuyPlan.requisition_id is NOT NULL, so log_activity always resolves the company from
    # the requisition (req -> customer_site -> company) — the row lands on the customer
    # timeline without extra resolution here.
    log_activity(
        db,
        activity_type=activity_type,
        user_id=user.id,
        buy_plan_id=plan.id,
        requisition_id=plan.requisition_id,
        description=description,
    )


# ── Workflow: SO Verification ────────────────────────────────────────


def verify_so(
    plan_id: int,
    action: str,
    user: User,
    db: Session,
    *,
    rejection_note: str | None = None,
) -> BuyPlan:
    """Ops verifies (or rejects/halts) the Sales Order in Acctivate.

    Approve → so_status=approved. Reject → so_status=rejected. Halt → plan.status=halted
    (stops everything).
    """
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.so_status != SOVerificationStatus.PENDING.value:
        raise ValueError(f"SO already verified (status: {plan.so_status})")
    if plan.status == BuyPlanStatus.HALTED.value:
        raise ValueError("Plan is halted")

    member = db.query(VerificationGroupMember).filter_by(user_id=user.id, is_active=True).first()
    if not member:
        raise PermissionError("User is not in the ops verification group")

    now = datetime.now(timezone.utc)
    plan.so_verified_by_id = user.id
    plan.so_verified_at = now

    if action == "approve":
        plan.so_status = SOVerificationStatus.APPROVED.value
        logger.info("SO verified for plan {} by {}", plan_id, user.email)
    elif action == "reject":
        plan.so_status = SOVerificationStatus.REJECTED.value
        plan.so_rejection_note = rejection_note
        logger.info("SO rejected for plan {}: {}", plan_id, rejection_note)
    elif action == "halt":
        # SOVerificationStatus has no dedicated HALTED value, so a halt reuses REJECTED
        # for so_status; it is distinguished from a plain reject by plan.status == HALTED
        # (set just below).
        # If the plan is still PENDING when halted, close its open engine request BEFORE the
        # transition so no REQUESTED row is orphaned in the approvals queue/badge and the
        # plan can never be resurrected by approving a stale request (the canceller is an
        # ops member who may be neither the submitter nor a manager/admin, so the helper
        # cancels on behalf of each request's own requester/owner — authz always satisfied).
        if plan.status == BuyPlanStatus.PENDING.value:
            _cancel_open_engine_requests_for_plan(plan, user, db)
        plan.so_status = SOVerificationStatus.REJECTED.value
        plan.so_rejection_note = rejection_note
        plan.status = BuyPlanStatus.HALTED.value
        plan.halted_by_id = user.id
        plan.halted_at = now
        logger.info("Plan {} HALTED by {}: {}", plan_id, user.email, rejection_note)
    else:
        raise ValueError(f"Invalid SO verification action: {action}")

    db.flush()
    return plan


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
    line.po_confirmed_at = datetime.now(timezone.utc)
    line.status = BuyPlanLineStatus.PENDING_VERIFY.value
    logger.info("PO {} confirmed for line {} (plan {})", po_number, line_id, plan_id)

    db.flush()
    return line


def verify_po(
    plan_id: int,
    line_id: int,
    action: str,
    user: User,
    db: Session,
    *,
    rejection_note: str | None = None,
) -> BuyPlanLine:
    """Ops verifies a PO was properly entered.

    Approve → line verified. Reject → back to awaiting_po. After approval, checks if all
    lines are done → auto-complete.
    """
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")

    line = db.get(BuyPlanLine, line_id)
    if not line or line.buy_plan_id != plan_id:
        raise ValueError(f"Line {line_id} not found in plan {plan_id}")
    if line.status != BuyPlanLineStatus.PENDING_VERIFY.value:
        raise ValueError(f"Line must be pending verification (current: {line.status})")

    member = db.query(VerificationGroupMember).filter_by(user_id=user.id, is_active=True).first()
    if not member:
        raise PermissionError("User is not in the ops verification group")

    now = datetime.now(timezone.utc)
    if action == "approve":
        line.status = BuyPlanLineStatus.VERIFIED.value
        line.po_verified_by_id = user.id
        line.po_verified_at = now
        logger.info("PO verified for line {} (plan {})", line_id, plan_id)
        check_completion(plan_id, db)
    elif action == "reject":
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


# ── Workflow: Completion ─────────────────────────────────────────────


def _complete_plan(plan: BuyPlan, db: Session) -> None:
    """Mark a plan completed and generate its case report.

    Shared by check_completion (normal auto-complete) and the stock-sale auto-complete
    job so both completion paths produce a case report.
    """
    plan.status = BuyPlanStatus.COMPLETED.value
    plan.completed_at = datetime.now(timezone.utc)
    plan.case_report = generate_case_report(plan, db)


def check_completion(plan_id: int, db: Session) -> BuyPlan:
    """Auto-complete the buy plan if all lines are in terminal state.

    Completion requires:
    - Plan is active
    - All lines are verified or cancelled
    - SO is verified (so_status = approved)
    """
    plan = db.get(
        BuyPlan,
        plan_id,
        options=[joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer)],
    )
    if not plan or plan.status != BuyPlanStatus.ACTIVE.value:
        return plan

    if not plan.lines:
        return plan

    terminal = {BuyPlanLineStatus.VERIFIED.value, BuyPlanLineStatus.CANCELLED.value}
    all_terminal = all(line.status in terminal for line in plan.lines)

    if all_terminal and plan.so_status == SOVerificationStatus.APPROVED.value:
        _complete_plan(plan, db)
        logger.info("Buy plan {} auto-completed (all lines terminal)", plan_id)
        db.flush()
        # Feed the proactive backbone from this confirmed customer purchase (best-effort).
        try:
            from app.services.purchase_history_service import record_buyplan_purchase_history

            record_buyplan_purchase_history(db, plan)
        except Exception:  # noqa: BLE001 — CPH must never break completion
            logger.exception("BUYPLAN_CPH: failed to record purchase history for plan {}", plan_id)
        db.flush()

    return plan


RESUBMITTABLE_STATUSES = {BuyPlanStatus.HALTED.value, BuyPlanStatus.CANCELLED.value}


def reset_buy_plan_to_draft(plan_id: int, user: User, db: Session) -> BuyPlan:
    """Reset a halted/cancelled buy plan back to draft for resubmission."""
    plan = db.get(BuyPlan, plan_id, options=[joinedload(BuyPlan.lines)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")

    if plan.status not in RESUBMITTABLE_STATUSES:
        raise ValueError(f"Only halted/cancelled plans can be resubmitted (current: {plan.status})")

    plan.status = BuyPlanStatus.DRAFT.value
    plan.so_status = SOVerificationStatus.PENDING.value
    plan.auto_approved = False
    plan.approved_by_id = None
    plan.approved_at = None
    plan.approval_notes = None
    plan.so_verified_by_id = None
    plan.so_verified_at = None
    plan.so_rejection_note = None
    plan.halted_by_id = None
    plan.halted_at = None
    plan.cancelled_at = None
    plan.cancelled_by_id = None
    plan.cancellation_reason = None
    plan.updated_at = datetime.now(timezone.utc)

    db.flush()
    logger.info("Buy plan {} reset to draft by user {}", plan_id, user.id)
    return plan


def cancel_buy_plan(plan_id: int, user: User, db: Session, *, reason: str | None = None) -> BuyPlan:
    """Cancel a buy plan and cascade-cancel its still-open lines.

    Open lines (awaiting_po / pending_verify) move to cancelled so no buyer task or PO
    nudge lingers. Completed or already-cancelled plans cannot be cancelled. The caller
    commits and dispatches notify_cancelled.
    """
    plan = db.get(BuyPlan, plan_id, options=[joinedload(BuyPlan.lines)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status in (BuyPlanStatus.COMPLETED.value, BuyPlanStatus.CANCELLED.value):
        raise ValueError(f"Cannot cancel plan in '{plan.status}' status")

    # If the plan is still PENDING, close its open engine request BEFORE the transition so
    # no REQUESTED row is orphaned in the approvals queue/badge — and, critically, so an
    # approver can no longer pull a stale request out of the queue and resurrect this
    # cancelled plan to ACTIVE. The helper cancels on behalf of each request's own
    # requester/owner, so the engine cancel authz is satisfied even when the canceller is
    # the (non-manager) plan owner.
    if plan.status == BuyPlanStatus.PENDING.value:
        _cancel_open_engine_requests_for_plan(plan, user, db)

    plan.status = BuyPlanStatus.CANCELLED.value
    plan.cancelled_at = datetime.now(timezone.utc)
    plan.cancelled_by_id = user.id
    plan.cancellation_reason = reason

    open_states = {BuyPlanLineStatus.AWAITING_PO.value, BuyPlanLineStatus.PENDING_VERIFY.value}
    cancelled_lines = 0
    for line in plan.lines:
        if line.status in open_states:
            line.status = BuyPlanLineStatus.CANCELLED.value
            cancelled_lines += 1

    logger.info(
        "Buy plan {} cancelled by {} ({} open line(s) cancelled): {}",
        plan_id,
        user.email,
        cancelled_lines,
        reason,
    )
    db.flush()
    return plan


def resubmit_buy_plan(
    plan_id: int,
    sales_order_number: str,
    user: User,
    db: Session,
    *,
    customer_po_number: str | None = None,
    salesperson_notes: str | None = None,
) -> BuyPlan:
    """Resubmit a rejected buy plan. Resets SO verification and approval.

    Used after manager rejection (plan back in draft).
    """
    plan = db.get(BuyPlan, plan_id, options=[joinedload(BuyPlan.lines)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.DRAFT.value:
        raise ValueError(f"Can only resubmit draft plans (current: {plan.status})")

    # Reset SO verification
    plan.so_status = SOVerificationStatus.PENDING.value
    plan.so_verified_by_id = None
    plan.so_verified_at = None
    plan.so_rejection_note = None

    # Reset approval
    plan.auto_approved = False
    plan.approved_by_id = None
    plan.approved_at = None
    plan.approval_notes = None

    # Update references
    plan.sales_order_number = sales_order_number
    plan.customer_po_number = customer_po_number
    plan.submitted_by_id = user.id
    plan.submitted_at = datetime.now(timezone.utc)
    plan.salesperson_notes = salesperson_notes

    # Auto-approve decision (same logic as initial submit)
    if _should_auto_approve(plan):
        plan.status = BuyPlanStatus.ACTIVE.value
        plan.auto_approved = True
        plan.approved_at = datetime.now(timezone.utc)
    else:
        plan.status = BuyPlanStatus.PENDING.value
        # Re-open the engine gate. Cancels the stale request from the prior submission so
        # exactly ONE REQUESTED request exists for this plan (RISK 2).
        _open_engine_request_for_plan(plan, user, db)

    db.flush()
    return plan


# ── Helpers: Buyer Task Generation ────────────────────────────────────


def _generate_buyer_tasks(plan: BuyPlan, db: Session) -> None:
    """Create 'Cut PO' tasks for each assigned buyer line when plan goes active.

    Each line is handled independently: a failure on one line is logged with the
    plan/line id and the loop continues, so one bad line never silently drops the
    whole batch.
    """
    from app.services.task_service import on_buy_plan_assigned

    for line in plan.lines:
        if not line.buyer_id:
            continue
        vendor_name = ""
        mpn = ""
        if line.offer:
            vendor_name = line.offer.vendor_name or ""
            mpn = line.offer.mpn or ""
        elif line.requirement:
            mpn = line.requirement.primary_mpn or ""
        try:
            on_buy_plan_assigned(
                db,
                requisition_id=plan.requisition_id,
                buyer_id=line.buyer_id,
                vendor_name=vendor_name,
                mpn=mpn,
                line_id=line.id,
            )
        except Exception:
            logger.warning("Buyer task auto-gen failed for plan {} line {}", plan.id, line.id, exc_info=True)


# ── Helpers: Auto-Approval ───────────────────────────────────────────


def _should_auto_approve(plan: BuyPlan) -> bool:
    """Decide whether a buy plan should be auto-approved.

    Auto-approves when total cost < threshold AND no critical AI flags. Used by both
    submit_buy_plan() and resubmit_buy_plan().
    """
    total = float(plan.total_cost or 0)
    has_critical = any(
        (f.get("severity") if isinstance(f, dict) else getattr(f, "severity", None)) == "critical"
        for f in (plan.ai_flags or [])
    )
    return total < settings.buyplan_auto_approve_threshold and not has_critical


# ── Helpers: Line Edits ──────────────────────────────────────────────


def _apply_line_edits(plan: BuyPlan, edits: list[dict], db: Session):
    """Replace AI-generated lines with salesperson's vendor swaps/splits."""
    edits_by_req: dict[int, list[dict]] = {}
    for edit in edits:
        edits_by_req.setdefault(edit["requirement_id"], []).append(edit)

    affected = set(edits_by_req.keys())
    to_remove = [ln for ln in plan.lines if ln.requirement_id in affected]
    for line in to_remove:
        plan.lines.remove(line)

    for req_id, req_edits in edits_by_req.items():
        requirement = db.get(Requirement, req_id)
        for edit in req_edits:
            offer = db.get(Offer, edit["offer_id"])
            if not offer:
                raise ValueError(f"Offer {edit['offer_id']} not found")

            unit_cost = float(offer.unit_price) if offer.unit_price else None
            unit_sell = float(requirement.target_price) if requirement and requirement.target_price else None
            margin_pct = None
            if unit_sell and unit_cost and unit_sell > 0:
                margin_pct = round(((unit_sell - unit_cost) / unit_sell) * 100, 2)

            buyer, reason = assign_buyer(offer, offer.vendor_card, db)
            ai_score = score_offer(offer, requirement, offer.vendor_card) if requirement else None

            new_line = BuyPlanLine(
                requirement_id=req_id,
                offer_id=offer.id,
                quantity=edit["quantity"],
                unit_cost=unit_cost,
                unit_sell=unit_sell,
                margin_pct=margin_pct,
                ai_score=ai_score,
                buyer_id=buyer.id if buyer else None,
                assignment_reason=reason,
                status=BuyPlanLineStatus.AWAITING_PO.value,
                sales_note=edit.get("sales_note"),
            )
            plan.lines.append(new_line)

    _recalculate_financials(plan)


def _apply_line_overrides(plan: BuyPlan, overrides: list[dict], db: Session):
    """Apply manager's line-level overrides (vendor swap, quantity, notes)."""
    for ovr in overrides:
        line = next((ln for ln in plan.lines if ln.id == ovr["line_id"]), None)
        if not line:
            logger.warning("Override line_id {} not found in plan {}", ovr["line_id"], plan.id)
            continue

        if ovr.get("offer_id"):
            offer = db.get(Offer, ovr["offer_id"])
            if offer:
                line.offer_id = offer.id
                line.unit_cost = float(offer.unit_price) if offer.unit_price else None
                if line.unit_sell and line.unit_cost and float(line.unit_sell) > 0:
                    line.margin_pct = round(
                        ((float(line.unit_sell) - float(line.unit_cost)) / float(line.unit_sell)) * 100, 2
                    )

        if ovr.get("quantity"):
            line.quantity = ovr["quantity"]

        if ovr.get("manager_note"):
            line.manager_note = ovr["manager_note"]

    _recalculate_financials(plan)


def _recalculate_financials(plan: BuyPlan):
    """Recompute plan-level cost, revenue, margin from lines."""
    total_cost = 0.0
    total_revenue = 0.0
    for line in plan.lines:
        if line.unit_cost and line.quantity:
            total_cost += float(line.unit_cost) * line.quantity
        if line.unit_sell and line.quantity:
            total_revenue += float(line.unit_sell) * line.quantity

    plan.total_cost = round(total_cost, 2) if total_cost else None
    plan.total_revenue = round(total_revenue, 2) if total_revenue else None
    if total_revenue > 0:
        plan.total_margin_pct = round(((total_revenue - total_cost) / total_revenue) * 100, 2)


def _is_stock_sale(plan: BuyPlan, db: Session) -> bool:
    """Detect stock/internal sales by vendor name match against config."""
    stock_names = settings.stock_sale_vendor_names
    if not plan.lines:
        return False
    for line in plan.lines:
        offer = line.offer or (db.get(Offer, line.offer_id) if line.offer_id else None)
        if not offer:
            return False
        vendor = (offer.vendor_name or "").strip().lower()
        if vendor not in stock_names:
            return False
    return True


# ── Intelligence: Favoritism Detection ─────────────────────────────


def detect_favoritism(salesperson_id: int, db: Session) -> list[dict]:
    """Detect if a salesperson disproportionately routes work to specific buyers.

    Looks at all completed/active V3 buy plans submitted by this salesperson
    and calculates buyer assignment distribution. Flags if any buyer receives
    more than the configured threshold percentage.

    Returns list of findings: [{buyer_id, buyer_name, pct, plan_count, severity}]
    """
    threshold = settings.buyplan_favoritism_threshold_pct

    # Get all plans by this salesperson
    plans = (
        db.query(BuyPlan)
        .filter(
            BuyPlan.submitted_by_id == salesperson_id,
            BuyPlan.status.in_(
                [
                    BuyPlanStatus.ACTIVE.value,
                    BuyPlanStatus.COMPLETED.value,
                    BuyPlanStatus.PENDING.value,
                ]
            ),
        )
        .options(joinedload(BuyPlan.lines))
        .all()
    )
    if len(plans) < 3:
        return []  # not enough data to detect patterns

    # Count lines per buyer
    buyer_counts: dict[int, int] = {}
    total_lines = 0
    for plan in plans:
        for line in plan.lines or []:
            if line.buyer_id:
                buyer_counts[line.buyer_id] = buyer_counts.get(line.buyer_id, 0) + 1
                total_lines += 1

    if total_lines == 0:
        return []

    buyers_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(buyer_counts.keys())).all()}
    findings = []
    for buyer_id, count in buyer_counts.items():
        pct = round(count / total_lines * 100, 1)
        if pct >= threshold:
            buyer = buyers_by_id.get(buyer_id)
            findings.append(
                {
                    "buyer_id": buyer_id,
                    "buyer_name": buyer.name if buyer else "Unknown",
                    "pct": pct,
                    "line_count": count,
                    "total_lines": total_lines,
                    "plan_count": len(plans),
                    "severity": "warning",
                    "message": (
                        f"{buyer.name if buyer else 'Unknown'} receives {pct}% of "
                        f"line assignments ({count}/{total_lines} lines across "
                        f"{len(plans)} plans)"
                    ),
                }
            )

    return findings


# ── Intelligence: Case Report ──────────────────────────────────────


def generate_case_report(plan: BuyPlan, db: Session) -> str:
    """Generate a structured case report when a buy plan completes.

    Captures: deal metadata, margin analysis, vendor selection, timeline,
    issue tracking. Stored in plan.case_report for post-deal analysis.
    """
    now = datetime.now(timezone.utc)
    lines = plan.lines or []
    quote = db.get(Quote, plan.quote_id) if plan.quote_id else None

    # ── Customer info
    customer = "Unknown"
    quote_number = "—"
    if quote:
        quote_number = quote.quote_number or "—"
        if quote.customer_site:
            site = quote.customer_site
            co = site.company if hasattr(site, "company") and site.company else None
            customer = co.name if co else (site.site_name or "Unknown")

    # ── Financials
    total_cost = float(plan.total_cost or 0)
    total_revenue = float(plan.total_revenue or 0)
    margin_pct = float(plan.total_margin_pct or 0)

    # ── Vendor breakdown
    vendor_lines: dict[str, list] = {}
    for line in lines:
        offer = line.offer or (db.get(Offer, line.offer_id) if line.offer_id else None)
        vendor = offer.vendor_name if offer else "Unknown"
        vendor_lines.setdefault(vendor, []).append(line)

    vendor_summary = []
    for vendor, vlines in vendor_lines.items():
        v_cost = sum(float(ln.unit_cost or 0) * (ln.quantity or 0) for ln in vlines)
        v_qty = sum(ln.quantity or 0 for ln in vlines)
        vendor_summary.append(f"  - {vendor}: {len(vlines)} lines, {v_qty:,} pcs, ${v_cost:,.2f}")

    # ── Timeline
    def _tz_aware(dt):
        """Ensure datetime is UTC-aware for safe subtraction."""
        if dt and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    timeline = []
    created = _tz_aware(plan.created_at)
    submitted = _tz_aware(plan.submitted_at)
    approved = _tz_aware(plan.approved_at)
    completed = _tz_aware(plan.completed_at)

    if created and submitted:
        days = (submitted - created).days
        timeline.append(f"  Build → Submit: {days} day{'s' if days != 1 else ''}")
    if submitted and approved:
        days = (approved - submitted).days
        hrs = int((approved - submitted).total_seconds() / 3600)
        timeline.append(f"  Submit → Approve: {hrs}h ({days}d)")
    if approved and completed:
        days = (approved - completed).days if completed > approved else (completed - approved).days
        timeline.append(f"  Approve → Complete: {abs(days)} day{'s' if abs(days) != 1 else ''}")
    if created and completed:
        total_days = (completed - created).days
        timeline.append(f"  Total cycle: {total_days} day{'s' if total_days != 1 else ''}")

    # ── PO timing (avg days from approval to PO confirm)
    po_times = []
    for line in lines:
        if line.po_confirmed_at and approved:
            delta = (_tz_aware(line.po_confirmed_at) - approved).total_seconds() / 3600
            po_times.append(delta)
    avg_po_hrs = round(sum(po_times) / len(po_times), 1) if po_times else None

    # ── Issues encountered
    issues = []
    for line in lines:
        if line.issue_type:
            offer = line.offer
            mpn = offer.mpn if offer else "—"
            issues.append(f"  - {mpn}: {line.issue_type} — {line.issue_note or 'no note'}")

    # ── AI flags summary
    flag_lines = []
    for f in plan.ai_flags or []:
        if isinstance(f, dict):
            flag_lines.append(f"  - [{f.get('severity', '?')}] {f.get('type', '?')}: {f.get('message', '')}")

    # ── Rejections
    rejections = []
    if plan.so_rejection_note:
        rejections.append(f"  - SO rejected: {plan.so_rejection_note}")
    for line in lines:
        if line.po_rejection_note:
            rejections.append(f"  - PO rejected (line {line.id}): {line.po_rejection_note}")

    # ── Build report
    submitter = db.get(User, plan.submitted_by_id) if plan.submitted_by_id else None
    approver = db.get(User, plan.approved_by_id) if plan.approved_by_id else None

    report = f"""CASE REPORT — Buy Plan #{plan.id}
{"=" * 50}

DEAL OVERVIEW
  Customer: {customer}
  Quote: {quote_number}
  SO#: {plan.sales_order_number or "—"}
  Salesperson: {submitter.name if submitter else "—"}
  Approver: {approver.name if approver else ("Auto-approved" if plan.auto_approved else "—")}

FINANCIALS
  Total Cost: ${total_cost:,.2f}
  Total Revenue: ${total_revenue:,.2f}
  Margin: {margin_pct:.1f}%
  Lines: {len(lines)}

VENDORS ({len(vendor_lines)} total)
{chr(10).join(vendor_summary) if vendor_summary else "  None"}

TIMELINE
{chr(10).join(timeline) if timeline else "  No timeline data"}
  Avg PO turnaround: {f"{avg_po_hrs}h" if avg_po_hrs is not None else "—"}

AI FLAGS ({len(flag_lines)})
{chr(10).join(flag_lines) if flag_lines else "  None"}

ISSUES ({len(issues)})
{chr(10).join(issues) if issues else "  None"}

REJECTIONS ({len(rejections)})
{chr(10).join(rejections) if rejections else "  None"}

Generated: {now.strftime("%Y-%m-%d %H:%M UTC")}
"""
    return report.strip()


# ── Workflow: PO Verification Scanning ─────────────────────────────


async def verify_po_sent(plan: "BuyPlan", db: "Session") -> list[dict]:
    """Scan buyer's Outlook sent folder for PO emails matching each line.

    For each line with a po_number, searches Graph API for emails containing that PO
    number. Returns list of verification results per line.
    """
    from ..utils.graph_client import GraphClient
    from ..utils.token_manager import get_valid_token

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
            if found and line.status == BuyPlanLineStatus.PENDING_VERIFY.value:
                line.status = BuyPlanLineStatus.VERIFIED.value
                line.po_verified_at = datetime.now(timezone.utc)

            results.append(
                {
                    "line_id": line.id,
                    "po_number": line.po_number,
                    "found": found,
                    "message_count": len(messages),
                }
            )
        except Exception as e:
            logger.error("PO verification failed for line {}: {}", line.id, e)
            results.append({"line_id": line.id, "po_number": line.po_number, "found": False, "error": str(e)})

    # Use centralized completion check (respects SO verification requirement)
    check_completion(plan.id, db)

    # NOTE: flush (not commit) — the caller (PO-verify job) owns the transaction.
    db.flush()
    return results
