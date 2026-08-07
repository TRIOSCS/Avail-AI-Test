"""Buy Plans package core — the single APIRouter, role/gate helpers
(_can_supervise/_can_resource/_require_po_cutter), the completion notifier, plan detail,
and the retired-hub lens redirects. The GET triple {plan_id:int} → pipeline-archive →
{tab} is co-located HERE in its original relative order so route matching never depends
on __init__ import order.

W4.8 split of the 1,543-line app/routers/htmx/buy_plans.py — pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in __init__).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from ....constants import (
    BuyPlanStatus,
    UserRole,
)
from ....database import get_db
from ....dependencies import (
    get_buyplan_for_user,
    is_manager_or_admin,
    require_user,
)
from ....models import (
    BuyPlan,
    BuyPlanLine,
    User,
)
from ....services.buyplan_naming import summarize_top_flag
from ....template_env import template_response
from .._shared import _base_ctx, _is_ops_member

router = APIRouter(tags=["htmx-views"])


def _can_supervise(user: User, db: Session) -> bool:
    """True when the user may see cross-user (scope=all) deal data.

    Managers/admins and ops verification-group members qualify.
    """
    return user.role in (UserRole.MANAGER, UserRole.ADMIN) or _is_ops_member(user, db)


_PO_CUTTER_ROLES = (UserRole.BUYER, UserRole.MANAGER, UserRole.ADMIN)


def _can_resource(user: User) -> bool:
    """True when the user may re-source / claim buy-plan lines (a PO-cutter)."""
    return user.role in _PO_CUTTER_ROLES


def _require_po_cutter(user: User) -> None:
    """403 unless the user is an active PO-cutter (buyer/manager/admin)."""
    if not _can_resource(user) or not getattr(user, "is_active", True):
        raise HTTPException(403, "Only buyers and managers can re-source / claim lines")


async def _notify_if_completed(plan_id: int, just_completed: bool) -> None:
    """Fire the completion notification exactly once, driven by a caller-computed
    *just_completed* flag — NEVER by re-deriving it via a second ``check_completion``
    call.

    The auto-complete DECISION lives entirely at service depth: ``verify_po``,
    ``remove_buy_plan_line``, and ``bulk_edit_buy_plan_lines`` each call
    ``check_completion`` themselves right after mutating line state, so by the time
    control returns to the route the plan/line object already carries the answer in its
    (still in-session, pre-commit) ``.status``. Callers capture *just_completed* from
    that status BEFORE ``db.commit()`` (so an expired attribute after commit can't force
    a surprise re-fetch) and pass it here AFTER commit — re-scanning the plan's lines a
    second time here would be redundant work and, worse, a second opportunity to invoke
    completion side effects if the "already complete" short-circuit were ever weakened.
    Used identically by the verify-po, remove-line, and bulk-save-lines routes.
    """
    if not just_completed:
        return
    from ....services.buyplan_notifications import notify_completed, run_notify_bg

    await run_notify_bg(notify_completed, plan_id)


@router.get("/v2/partials/buy-plans/{plan_id:int}", response_class=HTMLResponse)
async def buy_plan_detail_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plan detail as HTML partial.

    The ``{plan_id:int}`` path convertor is load-bearing: it makes this route match ONLY
    integer segments, so the sibling retired-lens redirect ``/v2/partials/buy-plans/{tab}``
    (str) and the literal ``/pipeline-archive`` never shadow a numeric plan id and
    vice-versa.
    """
    bp = get_buyplan_for_user(
        db,
        user,
        plan_id,
        options=[
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.buyer),
            joinedload(BuyPlan.quote),
            joinedload(BuyPlan.requisition),
            joinedload(BuyPlan.submitted_by),
            joinedload(BuyPlan.approved_by),
        ],
    )

    from ....services.buyplan_workflow import can_edit_buy_plan_lines, plan_needs_approver_reason
    from ....services.prepayment_service import prepayment_state_for_lines

    lines = bp.lines or []

    # Editing surface (epics I/J/K). ``can_edit_lines`` is the SAME server-side gate the
    # add/edit/remove endpoints enforce, so the template hides the controls with the exact
    # predicate the POSTs check (never UI-only). ``can_manage_plan`` = owner-or-manager, the
    # gate the Cancel + SO-number endpoints enforce. Offers/requirements power the vendor
    # picker + add-line form; loaded only when the viewer can actually edit (no wasted query).
    can_edit_lines = can_edit_buy_plan_lines(user, bp)
    can_manage_plan = is_manager_or_admin(user) or (bp.requisition and bp.requisition.created_by == user.id)
    terminal = bp.status in (BuyPlanStatus.COMPLETED.value, BuyPlanStatus.CANCELLED.value)
    offers_by_requirement: dict[int, list] = {}
    plan_requirements: list = []
    if can_edit_lines:
        from ....constants import OfferStatus
        from ....models import Offer, Requirement

        plan_requirements = (
            db.query(Requirement).filter(Requirement.requisition_id == bp.requisition_id).order_by(Requirement.id).all()
        )
        active_offers = (
            db.query(Offer)
            .options(joinedload(Offer.vendor_card))
            .filter(Offer.requisition_id == bp.requisition_id, Offer.status == OfferStatus.ACTIVE.value)
            .order_by(Offer.unit_price)
            .all()
        )
        for off in active_offers:
            if off.requirement_id is not None:
                offers_by_requirement.setdefault(off.requirement_id, []).append(off)

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "bp": bp,
            "lines": lines,
            "is_ops_member": _is_ops_member(user, db),
            "can_resource": _can_resource(user),
            # Supervisors/ops resolve flagged-issue lines (the buyer who raised them can't).
            "can_supervise": _can_supervise(user, db),
            "user": user,
            # Line-editing gate (epic I) + owner/manager gate for Cancel + SO number (J/K).
            "can_edit_lines": can_edit_lines,
            "can_manage_plan": can_manage_plan,
            # Resume is manager-only and only meaningful on a halted plan (epic K).
            "can_resume": is_manager_or_admin(user) and bp.status == BuyPlanStatus.HALTED.value,
            # SO number is editable by owner/manager at any non-terminal status (epic J).
            "can_edit_so": can_manage_plan and not terminal,
            "offers_by_requirement": offers_by_requirement,
            "plan_requirements": plan_requirements,
            # Most-urgent flag reason so the indicator states the issue at first glance.
            "top_flag": summarize_top_flag(bp.ai_flags),
            # Why the plan is silently stalled for lack of a configured approver (or None).
            "no_approver_reason": plan_needs_approver_reason(bp, db),
            # Live prepayment state per line (badge #11 + button→pill #10), one batch query.
            "prepay_state": prepayment_state_for_lines(db, [ln.id for ln in lines]),
        }
    )
    return template_response("htmx/partials/buy_plans/detail.html", ctx)


# ── Retired hub lens redirects (registered AFTER the {plan_id:int} detail route so a
#    numeric plan id is never captured by the {tab} converter; pipeline-archive is a
#    literal and precedes {tab} so it is not swallowed as an unknown lens). The hub's
#    My Queue + Pipeline bodies retired into the Approvals Workspace (spec §11.1;
#    docs/APPROVALS_PARITY_CHECKLIST.md) — stale pushed URLs 308 onto their workspace
#    equivalents, matching the repo's retired-route precedent (routers/requisitions2.py).


@router.get("/v2/partials/buy-plans/pipeline-archive")
async def pipeline_archive_partial(
    user: User = Depends(require_user),
) -> RedirectResponse:
    """Retired Done-archive pager — 308 to the workspace Closed list (BP tab)."""
    return RedirectResponse("/v2/partials/approvals/buy-plans/list?show_closed=true", status_code=308)


@router.get("/v2/partials/buy-plans/{tab}")
async def buy_plans_tab_partial(
    tab: str,
    scope: str = "",
    user: User = Depends(require_user),
) -> RedirectResponse:
    """Retired hub lens bodies (``my-queue`` / ``pipeline``) — 308 to the workspace.

    Both lenses map onto the workspace's Buy Plans tab body: My Queue's role-aware rows
    live in every tab's "Needs your approval" group; the Pipeline board's stage story
    lives in the work list + SO-pane kanban. ``scope`` threads through to seed the
    list's Mine/All toggle. Any other value 404s (same contract as before retirement).
    """
    if tab.replace("-", "_") not in ("my_queue", "pipeline"):
        raise HTTPException(404, "Unknown buy-plans lens")
    target = "/v2/partials/approvals/buy-plans"
    if scope:
        target += f"?scope={scope}"
    return RedirectResponse(target, status_code=308)
