"""buyplan_state.py — Buy plan status state machine (W3 consolidation).

The single enforced transition table + writer for ``BuyPlan.status``, mirroring
``app/services/requisition_state.py`` for requisitions. Before W3 the graph lived
implicitly in inline guards scattered across the 9 status writers in
``buyplan_approval.py`` / ``buyplan_lines.py``; this table formalizes it so a new
writer cannot silently invent an edge. Writers keep their own intent-specific
pre-checks (their user-facing 400 messages are load-bearing in routers/tests) and
route the actual write through :func:`transition`, which re-validates against the
table — if a writer's guard and the table ever drift, the table wins.

Unlike ``requisition_state.transition`` this does NOT write an ActivityLog row:
every buy-plan writer already owns its audit logging (approval ActivityLog rows,
engine ApprovalEvents, plan-level stamps), so adding a STATUS_CHANGED row here
would be net-new audit behavior, not consolidation.

Called by: buyplan_workflow/buyplan_approval.py (8 lifecycle writers),
    buyplan_workflow/buyplan_lines.py (resource_line's completed→active reopen)
Depends on: constants (BuyPlanStatus) — pure logic otherwise
"""

from loguru import logger

from ...constants import BuyPlanStatus

# The formalized buy-plan graph (from → allowed to). Edges and their writers:
#   draft     → pending    submit_buy_plan / resubmit_buy_plan
#   pending   → active     _run_approve_side_effects (both approval paths)
#   pending   → draft      _run_reject_side_effects
#   pending   → halted     halt_plan (HALTABLE_STATUSES)
#   active    → halted     halt_plan
#   active    → completed  _complete_plan (via check_completion)
#   halted    → active     resume_plan
#   halted    → draft      reset_buy_plan_to_draft (RESUBMITTABLE_STATUSES)
#   cancelled → draft      reset_buy_plan_to_draft
#   completed → active     resource_line (backorder reopen: vendor cancelled post-close)
#   draft/pending/active/halted → cancelled   cancel_buy_plan
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    BuyPlanStatus.DRAFT.value: {BuyPlanStatus.PENDING.value, BuyPlanStatus.CANCELLED.value},
    BuyPlanStatus.PENDING.value: {
        BuyPlanStatus.ACTIVE.value,
        BuyPlanStatus.DRAFT.value,
        BuyPlanStatus.HALTED.value,
        BuyPlanStatus.CANCELLED.value,
    },
    BuyPlanStatus.ACTIVE.value: {
        BuyPlanStatus.HALTED.value,
        BuyPlanStatus.COMPLETED.value,
        BuyPlanStatus.CANCELLED.value,
    },
    BuyPlanStatus.HALTED.value: {
        BuyPlanStatus.ACTIVE.value,
        BuyPlanStatus.DRAFT.value,
        BuyPlanStatus.CANCELLED.value,
    },
    BuyPlanStatus.CANCELLED.value: {BuyPlanStatus.DRAFT.value},
    BuyPlanStatus.COMPLETED.value: {BuyPlanStatus.ACTIVE.value},
}


def transition(plan, new_status: str | BuyPlanStatus) -> None:
    """Validate and apply a buy-plan status transition (the ONLY status writer).

    Raises ValueError on an edge not in ``ALLOWED_TRANSITIONS``. Same-status calls
    are a no-op. The caller owns flush/commit and all side effects (stamps, task
    generation, teardown sweeps, notifications) — this function only guards and
    writes the status column.
    """
    old_val = plan.status
    new_val = new_status.value if isinstance(new_status, BuyPlanStatus) else new_status

    if old_val == new_val:
        return  # no-op

    allowed = ALLOWED_TRANSITIONS.get(old_val, set())
    if new_val not in allowed:
        raise ValueError(f"Invalid buy plan transition: {old_val} → {new_val} (allowed: {sorted(allowed)})")

    plan.status = new_val
    logger.debug("Buy plan {} status: {} → {}", plan.id, old_val, new_val)
