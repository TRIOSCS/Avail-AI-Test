"""Buy Plan — Approval/rejection + plan lifecycle: submit, approve, halt, resume, reset,
cancel, resubmit, and auto-completion.

Split from the former monolithic `buyplan_workflow.py` (P4.3) along the "approval/
rejection lifecycle" + "plan lifecycle (reset/cancel/resubmit)" seams — kept as one
module (matching `docs/CODE_AUDIT_AND_HARDENING_PLAN.md`'s `buyplan_approval` name)
because every state transition here shares the same engine-request/prepayment
teardown helpers (``_cancel_open_engine_requests_for_plan`` /
``_cancel_open_prepayment_requests_for_plan``).

Called by: routers/htmx/buy_plans.py, services/approvals/service.py (decide()
    dispatch), services/buyplan_hub.py, jobs/inventory_jobs.py, services/buyplan_po
    (verify_po's completion check, lazy import to avoid a cycle)
Depends on: buyplan_scoring, buyplan_po (_line_amount), buyplan_reports
    (generate_case_report), approvals service (lazy), models, config
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ...config import settings
from ...constants import UserRole
from ...models import (
    Offer,
    Requirement,
    User,
)
from ...models.buy_plan import (
    BuyPlan,
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
    VerificationGroupMember,
)
from ..buyplan_scoring import assign_buyer, score_offer
from .buyplan_po import _line_amount
from .buyplan_reports import generate_case_report

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
    plan.submitted_at = datetime.now(UTC)
    plan.salesperson_notes = salesperson_notes
    # Clear any prior approval decision (a previously-rejected plan re-enters the queue
    # clean — no stale approved_at/approval_notes carrying the old rejection forward).
    plan.approved_by_id = None
    plan.approved_at = None
    plan.approval_notes = None

    if line_edits:
        _apply_line_edits(plan, line_edits, db)

    plan.is_stock_sale = _is_stock_sale(plan, db)

    # Every plan goes to the one manager approval — no auto-approve (frozen scope).
    plan.status = BuyPlanStatus.PENDING.value
    logger.info("Buy plan {} submitted for approval (cost={:.2f})", plan_id, float(plan.total_cost or 0))
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
    from ...dependencies import can_approve_buy_plans

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
    now = datetime.now(UTC)
    if line_overrides:
        _apply_line_overrides(plan, line_overrides, db)
    plan.status = BuyPlanStatus.ACTIVE.value
    # Phase D — one approval absorbs SO verification: the single manager approval IS the
    # SO sign-off, so stamp so_status=APPROVED here. ``check_completion``'s
    # ``so_status == APPROVED`` gate then passes for every new approval with no separate
    # verify-SO step. (The retired verify-SO route used to stamp these so_verified fields.)
    plan.so_status = SOVerificationStatus.APPROVED.value
    plan.so_verified_by_id = user.id
    plan.so_verified_at = now
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
    plan.approved_at = datetime.now(UTC)
    plan.approval_notes = reason
    logger.info("Buy plan {} rejected by {}: {}", plan.id, user.email, reason)
    _log_approval_activity(plan, "reject", user, reason, db)


def _cancel_open_engine_requests_for_plan(plan: BuyPlan, user: User | None, db: Session) -> int:
    """Cancel every open (REQUESTED) BUY_PLAN ApprovalRequest for *plan* via the engine.

    The single point that closes a plan's engine gate when the plan leaves PENDING — called
    by ``_open_engine_request_for_plan`` (before opening a fresh request, RISK 2: never two
    live REQUESTED rows) AND by the non-decide transitions that take a plan out of PENDING
    (``cancel_buy_plan``, ``halt_plan``). Cancelling the open request there means no
    REQUESTED row is orphaned in the approvals queue/badge for a plan that no longer exists
    to approve — and, crucially, closes the resurrection vector (an approver can no longer
    pick a stale request out of the queue and re-activate a cancelled plan).

    Authz: each request is cancelled on behalf of its OWN ``requested_by`` (falling back to
    ``owner``), the user who originally submitted the plan — so the engine ``cancel`` authz
    (requester/owner OR manager/admin) is satisfied for EVERY transition caller, regardless
    of whether the user driving the transition is the submitter, a manager/admin, or an
    ops-group member who is neither (the ``halt_plan`` case). The plan-level audit of who
    cancelled/halted the plan is already captured on the plan + its activity log; this
    cancel is a system-driven consequence of the plan leaving PENDING, not a separate
    user-initiated cancel of someone else's request. ``user`` is used only as a final
    fallback actor when a request somehow carries no requester/owner.

    Returns the number of requests cancelled. Lazy imports avoid the circular import (the
    approvals service imports buyplan_workflow for the decide() dispatch).
    """
    from ...constants import (
        ApprovalRequestStatus,
        ApprovalSubjectType,
    )
    from ...models.approvals import ApprovalRequest
    from ..approvals.events import cancel as svc_cancel

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


def _cancel_open_prepayment_requests_for_plan(
    plan_id: int, db: Session, reason: str, *, line_ids: list[int] | None = None
) -> int:
    """Void every open (REQUESTED) PREPAYMENT ApprovalRequest for *plan_id* — the money-
    safety teardown sweep (simulation finding #2, Task 9 extended).

    THE RISK it closes: an open prepayment (wire) approval is otherwise never cancelled when
    its PO's plan dies, so a manager could approve a wire for a cancelled / halted /
    completed deal, or a re-sourced PO whose vendor changed underneath. This is called by
    every transition that takes a plan out of a payable state — ``cancel_buy_plan``,
    ``halt_plan``, ``_complete_plan`` (all completion paths) and ``resource_line`` — so no
    dangling wire authorisation survives the plan.

    Scope (join ApprovalRequest → Prepayment on subject_id):
      - ``line_ids is None`` (default) → PLAN scope (``Prepayment.buy_plan_id == plan_id``):
        cancelling / halting / completing a plan voids ALL its pending prepayments
        regardless of line — the whole deal is dead.
      - ``line_ids`` given → additionally narrow to ``Prepayment.buy_plan_line_id.in_(...)``
        so ONLY those lines' pending wires are voided. ``resource_line`` passes the actually
        re-sourced line ids: re-sourcing line A must NOT void a sibling line B's legitimate
        REQUESTED prepayment (only A's PO/vendor changed underneath).

    Two-part sweep on the SAME scope:
      1. REQUESTED requests → CANCELLED (the never-approved wires; idempotent — a second call
         finds nothing REQUESTED).
      2. APPROVED-but-unwired ``Prepayment``s → ``void`` (``voided_at``/``void_reason``,
         ``pay_token`` cleared) + a fire-and-forget ``notify_prepayment_voided`` DO-NOT-WIRE
         stand-down to accounting/AP. This closes the QA review's biggest residual money risk:
         an authorised (about-to-be-wired) prepayment otherwise survives its dead plan. A
         ``paid`` prepayment is NEVER touched — the wire already went out (no auto claw-back).

    Sets each swept request CANCELLED + ``resolved_at`` = now + ``resolution_note`` = *reason*
    (mirroring ``_cancel_open_engine_requests_for_plan``'s cancel mechanics) and returns the
    REQUESTED-request count (the void of approved prepayments is a side effect, not counted).
    No engine ``cancel`` event is used: this is a system-driven consequence of the plan dying
    (the plan-level audit records who cancelled / halted / completed it), not a user-initiated
    cancel, and the sweep carries no actor. Lazy imports avoid the circular import with the
    approvals + notification services.
    """
    from ...constants import ApprovalRequestStatus, ApprovalSubjectType, PrepaymentStatus
    from ...models.approvals import ApprovalRequest
    from ...models.quality_plan import Prepayment
    from ..prepayment_notifications import (
        notify_prepayment_voided,
        run_prepayment_notify_bg,
        schedule_prepayment_notify,
    )

    now = datetime.now(UTC)

    # (1) Cancel every open (REQUESTED) prepayment approval request.
    stmt = (
        select(ApprovalRequest)
        .join(Prepayment, Prepayment.id == ApprovalRequest.subject_id)
        .where(
            ApprovalRequest.subject_type == ApprovalSubjectType.PREPAYMENT,
            ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            Prepayment.buy_plan_id == plan_id,
        )
    )
    if line_ids is not None:
        stmt = stmt.where(Prepayment.buy_plan_line_id.in_(line_ids))
    open_requests = db.execute(stmt).scalars().all()
    for ar in open_requests:
        ar.status = ApprovalRequestStatus.CANCELLED
        ar.resolved_at = now
        ar.resolution_note = reason

    # (2) Void every APPROVED-but-unwired prepayment on the same scope + stand down AP.
    pp_stmt = select(Prepayment).where(
        Prepayment.status == PrepaymentStatus.APPROVED.value,
        Prepayment.buy_plan_id == plan_id,
    )
    if line_ids is not None:
        pp_stmt = pp_stmt.where(Prepayment.buy_plan_line_id.in_(line_ids))
    approved_prepayments = db.execute(pp_stmt).scalars().all()
    for pp in approved_prepayments:
        pp.status = PrepaymentStatus.VOID.value
        pp.voided_at = now
        pp.void_reason = reason
        pp.pay_token = None  # a dead link/token can no longer authorise a wire

    if open_requests or approved_prepayments:
        # Flush the sweep so it is durable and immediately visible to any subsequent read —
        # idempotent even under a no-autoflush session (a second sweep then correctly finds
        # nothing REQUESTED/APPROVED). All call sites flush afterward regardless, so this is free.
        db.flush()

    # Dispatch the stand-down AFTER the flush so the persisted void_reason is what AP reads.
    # Loop-aware fire-and-forget: never blocks the teardown transaction (best-effort notice).
    for pp in approved_prepayments:
        schedule_prepayment_notify(run_prepayment_notify_bg(notify_prepayment_voided, pp.id))

    if open_requests or approved_prepayments:
        logger.info(
            "Teardown of plan {}: cancelled {} pending + voided {} approved prepayment(s): {}",
            plan_id,
            len(open_requests),
            len(approved_prepayments),
            reason,
        )
    return len(open_requests)


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
    from ...constants import ApprovalGateType
    from ..approvals.routing import NoEligibleApproverError
    from ..approvals.service import create_request

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
    from ...constants import ActivityType
    from ..activity_service import log_activity

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


# ── Workflow: Halt (the single off-ramp) ─────────────────────────────

# A plan can be halted only while it is still in flight. COMPLETED/CANCELLED are terminal
# and HALTED is idempotent — halting any of those raises. (Preserves the reachability of
# the retired verify-SO halt, which fired while a plan was PENDING or ACTIVE.)
HALTABLE_STATUSES = {BuyPlanStatus.PENDING.value, BuyPlanStatus.ACTIVE.value}


def _can_halt(user: User, db: Session) -> bool:
    """True when *user* may halt a plan: a manager/admin OR an active ops-group member.

    Mirrors the router's ``_can_supervise`` predicate (role OR active
    VerificationGroupMember) so the standalone halt action carries the same authority the
    retired verify-SO halt did.
    """
    if user.role in (UserRole.MANAGER, UserRole.ADMIN):
        return True
    return db.query(VerificationGroupMember).filter_by(user_id=user.id, is_active=True).first() is not None


def halt_plan(plan_id: int, user: User, db: Session, *, reason: str | None = None) -> BuyPlan:
    """Halt an in-flight buy plan — the single, standalone halt path (Phase D).

    Extracted from the retired ``verify_so(action="halt")`` body and hardened: any open
    engine request is cancelled FIRST (so no REQUESTED row is orphaned in the approvals
    queue and the plan can never be resurrected by approving a stale request) — this covers
    the BUY_PLAN gate while PENDING, matching ``cancel_buy_plan`` — then the plan moves to
    HALTED. ``so_status`` is set to REJECTED
    (SOVerificationStatus has no dedicated HALTED value; the halt is distinguished by
    ``plan.status == HALTED``) and the supplied reason is stored on ``so_rejection_note``
    so the case report and salesperson notification carry it.

    Auth: supervisor/ops (manager·admin·active ops member — see :func:`_can_halt`). A
    halted plan is resubmittable via ``reset_buy_plan_to_draft``. The caller owns the
    commit.
    """
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if not _can_halt(user, db):
        raise PermissionError("Only a supervisor or ops member can halt a buy plan")
    if plan.status not in HALTABLE_STATUSES:
        raise ValueError(f"Cannot halt a {plan.status} plan")

    now = datetime.now(UTC)
    # Close any open engine gate BEFORE the transition so no REQUESTED row is orphaned in the
    # approvals queue/badge — and so a stale request can't be pulled from the queue to
    # resurrect this plan. Covers the BUY_PLAN gate while PENDING (the helper is a no-op
    # when none are open). Called
    # UNCONDITIONALLY, matching cancel_buy_plan: the canceller may be an ops member who is
    # neither submitter nor manager/admin, so the helper cancels on behalf of each request's
    # own requester/owner — engine-cancel authz always satisfied.
    _cancel_open_engine_requests_for_plan(plan, user, db)
    # Void any pending prepayment (wire) approval — a halted deal must not leave a wire an
    # approver could still authorise (finding #2, Task 9 extended).
    _cancel_open_prepayment_requests_for_plan(plan.id, db, "buy plan halted — prepayment voided")
    plan.so_status = SOVerificationStatus.REJECTED.value
    plan.so_rejection_note = reason
    plan.status = BuyPlanStatus.HALTED.value
    plan.halted_by_id = user.id
    plan.halted_at = now
    logger.info("Plan {} HALTED by {}: {}", plan_id, user.email, reason)

    db.flush()
    return plan


def plan_needs_approver_reason(plan: BuyPlan, db: Session) -> str | None:
    """Why *plan* is stalled for lack of a configured approver, else ``None``.

    A submitted plan stalls silently when no active user holds the approving right for its
    open gate: ``create_request`` raises ``NoEligibleApproverError``, which is only logged,
    and the plan sits invisibly (the owner no longer sees a PENDING plan and no approver
    exists to see it). This read-only check lets the UI surface it instead. Returns
    ``"buy_plan"`` (PENDING, no buy-plan approver) or ``"purchase_order"`` (ACTIVE with a
    cut PO awaiting per-line verification and no approver eligible for that line's dollar
    amount — Phase 3: the check is per PENDING_VERIFY line, not the plan total), else
    ``None``.
    """
    from ...constants import ApprovalGateType
    from ..approvals.routing import has_eligible_approver

    if plan.status == BuyPlanStatus.PENDING.value:
        if not has_eligible_approver(db, ApprovalGateType.BUY_PLAN):
            return "buy_plan"
    elif plan.status == BuyPlanStatus.ACTIVE.value:
        for line in plan.lines:
            if line.status == BuyPlanLineStatus.PENDING_VERIFY.value and not has_eligible_approver(
                db, ApprovalGateType.PURCHASE_ORDER, _line_amount(line)
            ):
                return "purchase_order"
    return None


# ── Workflow: Completion ─────────────────────────────────────────────


def _has_open_po_gate(plan: BuyPlan) -> bool:
    """True while any line's PO still awaits its per-line sign-off (PENDING_VERIFY).

    The plan-level "PO decision still open" predicate: ``_complete_plan`` refuses to
    complete while it holds, so no completion path (auto-complete OR the stock-sale job's
    direct call) can cancel-then-complete past an undecided PO.
    """
    return any(line.status == BuyPlanLineStatus.PENDING_VERIFY.value for line in plan.lines)


def _complete_plan(plan: BuyPlan, db: Session) -> None:
    """Mark a plan completed and generate its case report.

    Shared by check_completion (normal auto-complete) and the stock-sale auto-complete
    job so both completion paths produce a case report. Refuses (warn + return, no state
    change) while a line's PO decision is still open — callers that bypass
    ``check_completion``'s all-lines-terminal check (the stock-sale job) must not silently
    complete past an undecided PO.
    """
    if _has_open_po_gate(plan):
        logger.warning("Buy plan {} completion blocked: line PO(s) still pending verification", plan.id)
        return

    # Close any open engine gate BEFORE completing so no REQUESTED row is orphaned in the
    # approvals queue/badge. Mirrors cancel_buy_plan / halt_plan; a defensive no-op when
    # nothing is open (covers a stray BUY_PLAN-subject race). Cancels on behalf of each
    # request's own requester/owner (submitted_by is only a last-resort fallback actor).
    _cancel_open_engine_requests_for_plan(plan, plan.submitted_by, db)
    # A completed deal must not leave a pending wire request behind (finding #2, Task 9
    # extended). Fires from every completion path since both check_completion and the
    # stock-sale job route through _complete_plan.
    _cancel_open_prepayment_requests_for_plan(plan.id, db, "buy plan completed — pending prepayment voided")

    plan.status = BuyPlanStatus.COMPLETED.value
    plan.completed_at = datetime.now(UTC)
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
        except Exception:
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
    plan.updated_at = datetime.now(UTC)

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

    # Close any open engine gate BEFORE the transition so no REQUESTED row is orphaned in
    # the approvals queue/badge — and, critically, so an approver can no longer pull a stale
    # request out of the queue and resurrect this cancelled plan. This covers the BUY_PLAN
    # gate while PENDING;
    # the helper is a no-op when none are open. It cancels on behalf of each request's own
    # requester/owner, so the engine cancel authz is satisfied even when the canceller is the
    # (non-manager) plan owner.
    _cancel_open_engine_requests_for_plan(plan, user, db)
    # Void any pending prepayment (wire) approval for this plan — a cancelled deal must not
    # leave a wire an approver could still authorise (finding #2, Task 9 extended).
    _cancel_open_prepayment_requests_for_plan(plan.id, db, "buy plan cancelled — prepayment voided")

    plan.status = BuyPlanStatus.CANCELLED.value
    plan.cancelled_at = datetime.now(UTC)
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
    plan.submitted_at = datetime.now(UTC)
    plan.salesperson_notes = salesperson_notes

    # Every plan goes to the one manager approval — no auto-approve (frozen scope).
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
    """Recompute plan-level cost, revenue, margin from lines.

    Per-line gates use ``is not None`` (not bare truthiness) so a genuine ``0.0``
    unit_cost/unit_sell (e.g. a free-sample line) is included in the sum rather than
    silently skipped — matches the same "0 is a real value, not falsy" fix applied to
    the line-level writers in ``buyplan_lines.py``. The plan-level ``total_cost``/
    ``total_revenue`` collapse to ``None`` only when NO line ever contributed a real
    (non-None) value — tracked via ``has_cost``/``has_revenue`` rather than the running
    total's own truthiness, so a plan whose lines are ALL genuinely $0 (e.g. every line
    free-sample) reports ``0.0`` ("$0.00", a real fact) instead of ``None`` ("no data",
    which would be wrong — the data IS there, it's just zero).
    """
    total_cost = 0.0
    total_revenue = 0.0
    has_cost = False
    has_revenue = False
    for line in plan.lines:
        if line.unit_cost is not None and line.quantity:
            total_cost += float(line.unit_cost) * line.quantity
            has_cost = True
        if line.unit_sell is not None and line.quantity:
            total_revenue += float(line.unit_sell) * line.quantity
            has_revenue = True

    plan.total_cost = round(total_cost, 2) if has_cost else None
    plan.total_revenue = round(total_revenue, 2) if has_revenue else None
    if total_revenue > 0:
        plan.total_margin_pct = round(((total_revenue - total_cost) / total_revenue) * 100, 2)
    else:
        plan.total_margin_pct = None


# ── Workflow: Resume (manager un-halts a plan — epic K) ───────────────


def resume_plan(plan_id: int, user: User, db: Session) -> BuyPlan:
    """Resume a HALTED plan back to ACTIVE (manager-only).

    The manager who halted a deal can put it back in flight. The halt audit
    (``halted_by_id`` / ``halted_at``) is PRESERVED for history — resume is NOT a reset
    (``reset_buy_plan_to_draft`` nulls the audit + returns to DRAFT; do not confuse them).
    Caller commits.
    """
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if not _is_manager_or_admin(user):
        raise PermissionError("Only a manager can resume a halted buy plan.")
    if plan.status != BuyPlanStatus.HALTED.value:
        raise ValueError(f"Only a halted plan can be resumed (current: {plan.status}).")

    plan.status = BuyPlanStatus.ACTIVE.value
    plan.updated_at = datetime.now(UTC)
    # halted_by_id / halted_at are intentionally LEFT in place as the halt→resume audit trail.
    db.flush()
    logger.info("Buy plan {} RESUMED to active by {} (halt audit preserved)", plan_id, user.email)
    return plan


def _is_manager_or_admin(user: User) -> bool:
    """True for MANAGER/ADMIN (mirrors dependencies.is_manager_or_admin; inlined to
    avoid a service→dependencies import cycle).

    Duplicated (not imported) from ``buyplan_lines._is_manager_or_admin`` — both are the
    same one-line role check and importing across for a single boolean predicate is not
    worth a cross-module edge; keep in sync if the role set ever changes.
    """
    return user.role in (UserRole.MANAGER, UserRole.ADMIN)


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
