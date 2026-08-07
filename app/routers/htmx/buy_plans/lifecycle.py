"""Plan-level lifecycle actions — submit / approve (two-part handoff) / halt / cancel /
resume / reset / SO-number, plus the inline prepayment decide.

W4.8 split of the 1,543-line app/routers/htmx/buy_plans.py — pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in __init__).
"""

import secrets
from datetime import UTC, datetime

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ....database import get_db
from ....dependencies import (
    get_buyplan_for_user,
    is_manager_or_admin,
    require_buyplan_approver,
    require_user,
)
from ....models import (
    BuyPlan,
    User,
)
from ....services.stale_guard import StaleEditError, ensure_not_stale, stale_conflict_response
from .common import buy_plan_detail_partial, router

# The auto-filled note when a manager sends a plan back for sign-off without typing
# one — the engine requires a non-blank reject comment, and the change summary (the
# audit log since submission) always rides along on the pane (spec §7).
SEND_BACK_DEFAULT_NOTE = "Sent back for sign-off — see change summary"


def _workspace_pane_response(request: Request, user: User, db: Session, plan_id: int) -> HTMLResponse:
    """The shared origin=approvals_workspace re-render for plan lifecycle POSTs (halt /
    resume / cancel / reset — 2.5): the plan's deal pane in place + an awListRefresh
    nudge so the left work list repaints its status."""
    from ..approvals_hub import render_plan_pane

    resp = render_plan_pane(request, user, db, plan_id)
    resp.headers["HX-Trigger"] = "awListRefresh"
    return resp


@router.post("/v2/partials/approvals/prepay-requests/{request_id}/decide", response_class=HTMLResponse)
async def prepay_request_decide(
    request: Request,
    request_id: int,
    action: str = Form("approve"),
    comment: str | None = Form(None),
    origin: str = Form(""),
    hub_scope: str = Form("all"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Decide a prepayment ApprovalRequest from an inline action (HTML re-render).

    The standalone decision route (POST /v2/approvals/requests/{id}/decision) returns JSON;
    the inline callers instead need a refreshed body swapped in place. This thin sibling
    resolves the request via the SAME approvals-engine ``decide`` (no duplicated logic),
    then re-renders the caller's surface by ``origin``: ``approvals_workspace`` → the
    prepayment's pane + a list refresh; anything else → the workspace Prepayments tab
    body (``#ap-hub-body``). Reject requires a non-blank comment (400 otherwise); a caller
    who holds no PENDING recipient slot is 403 (engine PermissionError); a stale/decided
    request is 400 (engine ValueError).
    """
    from ....constants import ApprovalGateType
    from ....models.approvals import ApprovalRequest
    from ....services.approvals.service import decide as svc_decide

    if action == "reject" and not (comment or "").strip():
        raise HTTPException(400, "A reason is required to reject a prepayment.")

    # This route is prepayment-specific: refuse a non-PREPAYMENT request outright so it can
    # neither decide a foreign gate here nor mis-fire the OK-TO-WIRE notice against a wrong
    # subject_id (a buy-plan/quote subject_id is NOT a Prepayment id).
    ar = db.get(ApprovalRequest, request_id)
    if ar is None:
        raise HTTPException(404, "Prepayment request not found.")
    if ar.gate_type != ApprovalGateType.PREPAYMENT:
        raise HTTPException(400, "Not a prepayment approval request.")

    try:
        svc_decide(db, request_id, user, action, comment=comment or None)
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # Stamp the prepayment lifecycle + fan the accounting/AP notice. Fire-and-forget: the
    # runner isolates every error so a failed notice never breaks the decision that just
    # committed. APPROVE → approved + mint the single-use pay_token (the "OK TO WIRE" email
    # link); REJECT → void + the "DO NOT WIRE" stand-down.
    if ar.subject_id is not None and action in ("approve", "reject"):
        from ....constants import PrepaymentStatus
        from ....models.quality_plan import Prepayment
        from ....services.prepayment_notifications import (
            notify_prepayment_approved,
            notify_prepayment_voided,
            run_prepayment_notify_bg,
        )

        pp = db.get(Prepayment, ar.subject_id)
        if action == "approve":
            if pp is not None:
                pp.status = PrepaymentStatus.APPROVED.value
                pp.approved_by_id = user.id
                pp.approved_at = datetime.now(UTC)
                pp.pay_token = secrets.token_urlsafe(32)
                db.commit()
            await run_prepayment_notify_bg(notify_prepayment_approved, ar.subject_id)
        elif pp is not None:  # reject
            pp.status = PrepaymentStatus.VOID.value
            pp.void_reason = "rejected by approver"
            pp.voided_at = datetime.now(UTC)
            pp.voided_by_id = user.id
            # Note-to-the-fixer (2.2): the (required) reject reason lands on the
            # prepayment's notes thread tagged with the decision. (The write-only
            # in-app Notification write was deleted, W2.9/§5.5.)
            from ....services.workspace_notes import add_note

            note_text = (comment or "").strip()
            if note_text:
                add_note(
                    db,
                    user=user,
                    body=note_text,
                    buy_plan_id=pp.buy_plan_id,
                    prepayment_id=pp.id,
                    decision="rejected",
                )
            db.commit()
            await run_prepayment_notify_bg(notify_prepayment_voided, pp.id)

    if origin == "approvals_workspace" and ar.subject_id is not None:
        # Workspace pane decide: re-render the prepayment's pane in place + repaint
        # the left list (awListRefresh), mirroring the SO/PO pane branches.
        from ..approvals_hub import render_prepayment_pane

        resp = render_prepayment_pane(request, user, db, int(ar.subject_id))
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp
    # Legacy-console origin ("approvals_hub") and originless posts both land on the
    # workspace Prepayments tab body — the only list surface these inline decisions
    # originate from since the Buy Plans hub retired (render_tab_body also resolves
    # the legacy "prepayment" tab key onto the same body).
    from ..approvals_hub import render_tab_body

    return render_tab_body(request, user, db, "prepayments", hub_scope)


@router.post("/v2/partials/buy-plans/{plan_id}/submit", response_class=HTMLResponse)
async def buy_plan_submit_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a draft buy plan with SO# — returns refreshed detail partial."""
    from ....services.buyplan_notifications import notify_submitted, run_notify_bg
    from ....services.buyplan_workflow import submit_buy_plan

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    so = form.get("sales_order_number", "").strip()
    if not so:
        raise HTTPException(400, "Sales Order # is required")

    try:
        plan = submit_buy_plan(
            plan_id,
            so,
            user,
            db,
            customer_po_number=form.get("customer_po_number") or None,
            salesperson_notes=form.get("salesperson_notes") or None,
        )
        db.commit()
        # Every submit goes to the manager gate — no auto-approve (frozen scope), so
        # submit always notifies SUBMITTED, never approved.
        await run_notify_bg(notify_submitted, plan.id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/approve", response_class=HTMLResponse)
async def buy_plan_approve_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_buyplan_approver),
    db: Session = Depends(get_db),
):
    """Approve or reject a pending buy plan — returns refreshed detail.

    Gated by ``require_buyplan_approver`` (403 unless the user holds the per-user
    can_approve_buy_plans right). Reject requires a reason (enforced in the service).

    QP Phase C1: the approval engine OWNS the gate. We look up the open BUY_PLAN
    ApprovalRequest for this plan and resolve it via the engine's ``decide`` — which drives
    the buy-plan side effects (ACTIVE + buyer tasks / DRAFT) in the SAME transaction. We let
    ``decide`` raise (no swallowing) so a side-effect failure rolls back the whole decision
    atomically (RISK 1). If NO open request exists — a plan that went PENDING before C1
    deployed — we fall back to the legacy ``approve_buy_plan`` and log a WARNING (RISK 3,
    transition window; the fallback is removed in a follow-up once no pre-C1 plans remain).
    """
    from sqlalchemy import select as _select

    from ....constants import ApprovalRequestStatus, ApprovalSubjectType
    from ....models.approvals import ApprovalRequest
    from ....services.approvals.service import decide as svc_decide
    from ....services.buyplan_notifications import (
        notify_approved,
        notify_rejected,
        run_notify_bg,
    )
    from ....services.buyplan_workflow import approve_buy_plan
    from ....services.workspace_notes import add_note

    form = await request.form()
    action = form.get("action", "approve")
    origin = form.get("origin", "")
    hub_scope = form.get("hub_scope", "all")
    notes = (form.get("notes") or "").strip() or None

    # Two-part approve (spec §7 / workspace 2.2): the workspace approval block posts a
    # handoff instead of a bare action — proceed → the existing approve path; send_back
    # → the existing reject→draft transition ("send back for sign-off"). The engine
    # requires a non-blank reject comment, so a blank send-back note auto-fills.
    handoff = (form.get("handoff") or "").strip()
    if handoff == "proceed":
        action = "approve"
    elif handoff == "send_back":
        action = "reject"
        if not notes:
            notes = SEND_BACK_DEFAULT_NOTE
    decision_tag = None
    if action == "reject":
        decision_tag = "sent_back" if handoff == "send_back" else "rejected"

    open_request = (
        db.execute(
            _select(ApprovalRequest).where(
                ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
                ApprovalRequest.subject_id == plan_id,
                ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            )
        )
        .scalars()
        .first()
    )

    try:
        if open_request is not None:
            # Engine path: decide() resolves the request AND drives the plan side effects.
            svc_decide(db, open_request.id, user, action, comment=notes or None)
        else:
            # RISK 3 fallback: plan pending pre-C1 with no engine request yet. Still
            # load-bearing for the pre-engine PENDING plans (owner decision pending:
            # backfill an ApprovalRequest vs reset+resubmit — Packet 3); deletable
            # only after that decision lands.
            logger.warning(
                "LEGACY APPROVAL FALLBACK FIRED: buy plan {} is PENDING with no open engine "
                "ApprovalRequest (pre-engine plan) — deciding via legacy approve_buy_plan",
                plan_id,
            )
            approve_buy_plan(plan_id, action, user, db, notes=notes)
        db.commit()
        if action == "approve":
            await run_notify_bg(notify_approved, plan_id)
        else:
            await run_notify_bg(notify_rejected, plan_id)
    except PermissionError as e:
        # The dependency already 403s unauthorized callers; this maps the service's
        # defense-in-depth approval-right check to 403 (not 400) if it is ever reached.
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # Post-decision fan-out (2.2) — after the decision committed, never inside it:
    #   reject / send-back → the note-to-the-fixer lands on the plan's notes thread
    #   tagged with the decision. (The approve-with-changes summary and the reject
    #   in-app pings wrote only write-only Notification rows — deleted, W2.9/§5.5;
    #   the submitter still gets the reject/approve email via run_notify_bg above.)
    if action == "reject" and notes:
        add_note(db, user=user, body=notes, buy_plan_id=plan_id, decision=decision_tag)
        db.commit()

    if origin == "approvals_workspace":
        # Workspace pane decide: re-render THIS plan's pane in place and nudge the
        # left work list to repaint (awListRefresh — the split shell's list container
        # listens for it), so the decided row leaves the Needs-your-approval group.
        from ..approvals_hub import render_plan_pane

        resp = render_plan_pane(request, user, db, plan_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp
    if origin == "approvals_hub":
        from ..approvals_hub import render_tab_body

        return render_tab_body(request, user, db, "buy-plan", hub_scope)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/halt", response_class=HTMLResponse)
async def buy_plan_halt_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Halt an in-flight buy plan — the standalone off-ramp (Phase D).

    Auth is enforced in the service (``halt_plan`` raises PermissionError unless the user
    is a supervisor/ops member → mapped to 403 here). Reuses ``notify_so_rejected`` with
    ``action="halt"`` so the salesperson still gets the halt + reason notification.
    """
    from ....services.buyplan_notifications import notify_so_rejected, run_notify_bg
    from ....services.buyplan_workflow import halt_plan

    form = await request.form()
    origin = form.get("origin", "")

    # A halt is an off-ramp on a money-governing deal — the reason is required so the case
    # report + salesperson notification always say WHY (stored on so_rejection_note; no column).
    reason = (form.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "A reason is required to halt a buy plan.")

    try:
        plan = halt_plan(plan_id, user, db, reason=reason)
        db.commit()
        await run_notify_bg(notify_so_rejected, plan.id, action="halt")
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if origin == "approvals_workspace":
        return _workspace_pane_response(request, user, db, plan_id)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/cancel", response_class=HTMLResponse)
async def buy_plan_cancel_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cancel a buy plan — delegates to the service (line cascade + notification).

    Gated to the plan owner (salesperson) or a manager/admin (epic K): a non-owner restricted
    role 404s at ``get_buyplan_for_user``; a non-owner non-manager (e.g. a buyer on someone
    else's plan) 403s. The cancellation reason is REQUIRED (400 if blank).
    """
    from ....services.buyplan_notifications import notify_cancelled, run_notify_bg
    from ....services.buyplan_workflow import cancel_buy_plan

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.requisition)])
    if not (is_manager_or_admin(user) or (plan.requisition and plan.requisition.created_by == user.id)):
        raise HTTPException(403, "Only the plan owner or a manager can cancel this buy plan.")

    form = await request.form()
    reason = (form.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "A reason is required to cancel a buy plan.")

    try:
        plan = cancel_buy_plan(plan_id, user, db, reason=reason)
        db.commit()
        await run_notify_bg(notify_cancelled, plan.id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if form.get("origin") == "approvals_workspace":
        return _workspace_pane_response(request, user, db, plan_id)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/resume", response_class=HTMLResponse)
async def buy_plan_resume_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Resume a HALTED plan back to ACTIVE — manager-only (epic K).

    Unlike Reset (which returns to DRAFT and nulls the halt audit), Resume preserves
    ``halted_by/at`` as the halt→resume history. The service raises PermissionError for a
    non-manager (→ 403) and ValueError for a non-halted plan (→ 400).
    """
    from ....services.buyplan_workflow import resume_plan

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    try:
        resume_plan(plan_id, user, db)
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    form = await request.form()
    if form.get("origin") == "approvals_workspace":
        return _workspace_pane_response(request, user, db, plan_id)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/so-number", response_class=HTMLResponse)
async def buy_plan_set_so_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set/edit the plan's active Sales Order number (epic J).

    Owner (salesperson) or manager, at any non-terminal status. A non-owner restricted
    role 404s; a non-owner non-manager 403s; a terminal plan 400s (service ValueError).
    """
    from ....services.buyplan_workflow import set_sales_order_number

    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.requisition)])
    if not (is_manager_or_admin(user) or (plan.requisition and plan.requisition.created_by == user.id)):
        raise HTTPException(403, "Only the plan owner or a manager can edit the Sales Order number.")

    form = await request.form()
    # Stale-edit guard (2.1): the narrowest edited object is the PLAN (SO# lives on it).
    try:
        ensure_not_stale(plan, form.get("expected_updated_at"))
    except StaleEditError:
        return stale_conflict_response()
    try:
        set_sales_order_number(plan_id, form.get("sales_order_number"), user, db)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/reset", response_class=HTMLResponse)
async def buy_plan_reset_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reset halted/cancelled plan to draft — returns refreshed detail."""
    from ....services.buyplan_workflow import reset_buy_plan_to_draft

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    try:
        reset_buy_plan_to_draft(plan_id, user, db)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    form = await request.form()
    if form.get("origin") == "approvals_workspace":
        return _workspace_pane_response(request, user, db, plan_id)

    return await buy_plan_detail_partial(request, plan_id, user, db)
