"""Per-line PO operations — confirm-po (QP-purchasing fold-in), re-source + claim
(vendor-cancel fall-down), verify-po, receive, flag/resolve issue.

W4.8 split of the 1,543-line app/routers/htmx/buy_plans.py — pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in __init__).
"""

from datetime import datetime

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from ....constants import (
    BuyPlanStatus,
    PaymentMethod,
)
from ....database import get_db
from ....dependencies import (
    get_buyplan_for_user,
    require_buyplan_po_approver,
    require_user,
)
from ....models import (
    BuyPlanLine,
    User,
)
from ....services.stale_guard import StaleEditError, ensure_not_stale, stale_conflict_response
from .common import _notify_if_completed, _require_po_cutter, buy_plan_detail_partial, router


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/confirm-po", response_class=HTMLResponse)
async def buy_plan_confirm_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer confirms PO — returns the refreshed detail partial (or, from the Approvals
    Workspace, the refreshed PO pane).

    Workspace additions: ``payment_method`` (validated against
    ``PO_LINE_PAYMENT_METHODS`` in the service) records the Acctivate PO terms;
    ``qp_purchasing_*`` fields fold the QP-purchasing answers (incl. AS9120B) onto the
    line's vendor QP row via ``qp_workspace.apply_qp_purchasing`` — the applied diff is
    field-audited. ``origin=approvals_workspace`` re-renders the PO pane + refreshes
    the work list.
    """
    from ....services.buyplan_notifications import notify_po_confirmed, run_notify_bg
    from ....services.buyplan_workflow import confirm_po
    from ....services.field_audit import diff_fields, log_field_edits
    from ....services.qp_workspace import apply_qp_purchasing

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    plan = get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    po_number = form.get("po_number", "").strip()
    ship_date_str = form.get("estimated_ship_date", "")
    payment_method = (form.get("payment_method") or "").strip() or None
    origin = form.get("origin", "")

    if not po_number:
        raise HTTPException(400, "PO number is required")

    ship_date = None
    if ship_date_str:
        try:
            ship_date = datetime.fromisoformat(ship_date_str)
        except ValueError:
            ship_date = datetime.now()
    else:
        ship_date = datetime.now()

    # Stale-edit guard (2.1): the narrowest edited object is the LINE being confirmed.
    target_line = db.get(BuyPlanLine, line_id)
    if target_line is not None and target_line.buy_plan_id == plan_id:
        try:
            ensure_not_stale(target_line, form.get("expected_updated_at"))
        except StaleEditError:
            return stale_conflict_response()

    # COD contradicts a live prepayment (money is already committed up front) — reject
    # here at the route so prepayment_service stays untouched by the confirm-PO flow.
    if payment_method == PaymentMethod.COD.value:
        from ....constants import PrepaymentStatus
        from ....models.quality_plan import Prepayment

        live_prepayment = db.scalars(
            select(Prepayment.id).where(
                Prepayment.buy_plan_line_id == line_id,
                Prepayment.status.in_(
                    (
                        PrepaymentStatus.REQUESTED.value,
                        PrepaymentStatus.APPROVED.value,
                        PrepaymentStatus.PAID.value,
                    )
                ),
            )
        ).first()
        if live_prepayment is not None:
            raise HTTPException(
                400,
                "This line has a prepayment in progress — COD terms would contradict it. "
                "Pick the prepaid method, or void the prepayment first.",
            )

    qp_fields = {key[len("qp_") :]: value for key, value in form.multi_items() if key.startswith("qp_")}

    # Field-audit (2.1): diff the line's PO fields BEFORE confirm_po mutates them, then
    # merge with the QP-purchasing diff into ONE row per save.
    line_updates: dict = {"po_number": po_number, "estimated_ship_date": ship_date}
    if payment_method is not None:
        line_updates["payment_method"] = payment_method
    line_edits = diff_fields(target_line, line_updates) if target_line is not None else []

    try:
        line = confirm_po(plan_id, line_id, po_number, ship_date, user, db, payment_method=payment_method)
        edits = list(line_edits)
        if qp_fields:
            _qp, qp_edits = apply_qp_purchasing(db, plan=plan, line=line, user=user, fields=qp_fields)
            edits.extend(qp_edits)
        log_field_edits(db, user=user, buy_plan_id=plan_id, buy_plan_line_id=line_id, edits=edits)
        db.commit()
        await run_notify_bg(notify_po_confirmed, plan_id, line_id=line_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if origin == "approvals_workspace":
        from ..approvals_hub import render_po_pane

        resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp

    return await buy_plan_detail_partial(request, plan_id, user, db)


async def _resource_lines_and_alert(
    plan_id: int,
    line_id: int,
    reason_code: str,
    reason_note: str | None,
    also_line_ids: list[int],
    user: User,
    db: Session,
) -> dict:
    """Shared fall-down → re-source core (vendor-cancel, including the completed-plan
    backorder case).

    Pools the target line(s) via the single ``resource_line`` engine, commits, and fans out
    one URGENT backfill alert per pooled line. Returns the service payload; raises HTTP 400
    on a service ValueError (with a server-side log first).

    Backorder emergency: ``resource_line`` reports ``was_completed`` when it had to reopen an
    already-COMPLETED plan (a vendor cancelled AFTER the deal closed). That flag is threaded
    into every ``notify_resource_requested`` dispatch so the broadcast forces email + Teams DM
    to ALL recipients regardless of their re-source-alert preference, with a BACKORDER subject.
    It MUST be passed (not re-derived): by notification time the plan is already reopened to
    ACTIVE, so the completed-at-cancel-time fact would be lost.
    """
    from ....services.buyplan_notifications import notify_resource_requested, run_notify_bg
    from ....services.buyplan_workflow import resource_line

    try:
        payload = resource_line(plan_id, line_id, reason_code, reason_note, user, db, also_line_ids=also_line_ids)
        db.commit()
    except ValueError as e:
        # Log before re-raising so a real failure (e.g. an un-keyable requirement deep in
        # the service) leaves a server trace instead of a silent, mislabeled 400.
        logger.warning("Re-source failed for plan {} line {}: {}", plan_id, line_id, e)
        raise HTTPException(400, str(e)) from e

    # Broadcast one urgent alert PER pooled line (scope=plan re-sources siblings too, and
    # each pooled line needs its own claim).
    was_completed = payload.get("was_completed", False)
    for resourced in payload["resourced_lines"]:
        await run_notify_bg(
            notify_resource_requested,
            plan_id,
            line_id=resourced["line_id"],
            actor_id=user.id,
            reason=reason_code,
            was_completed=was_completed,
        )
    return payload


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/resource", response_class=HTMLResponse)
async def buy_plan_resource_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Re-source a line whose vendor PO was cancelled (vendor-cancel fall-down).

    Records the cancellation (vendor performance), marks the offer sold + the vendor
    unavailable, drops the line into the open claim pool, and fires the URGENT backfill
    alert to all other buyers. ``scope=plan`` re-sources the plan's other cut lines too.
    Also the completed-plan BACKORDER entry point: when the target line sits on an
    already-COMPLETED plan (a vendor cancelled AFTER the deal closed), ``resource_line``
    reopens it to ACTIVE and the broadcast escalates to a forced EMERGENCY alert. ``origin``
    routes the re-render: ``approvals_workspace`` → the line's PO pane, ``approvals_hub``
    → the PO Approval tab body, else the full plan detail.
    """
    # Per-record ownership (non-owner SALES/TRADER → 404) + PO-cutter role gate (403).
    get_buyplan_for_user(db, user, plan_id)
    _require_po_cutter(user)

    form = await request.form()
    reason_code = form.get("reason_code", "").strip()
    reason_note = (form.get("reason_note") or "").strip() or None
    scope = form.get("scope", "line")
    also_line_ids = [int(i) for i in form.getlist("also_line_ids")] if scope == "plan" else []

    if not reason_code:
        raise HTTPException(400, "A re-source reason is required")

    origin = form.get("origin", "")
    hub_scope = form.get("hub_scope", "all")

    await _resource_lines_and_alert(plan_id, line_id, reason_code, reason_note, also_line_ids, user, db)

    if origin == "approvals_workspace":
        from ..approvals_hub import render_po_pane

        resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp
    if origin == "approvals_hub":
        from ..approvals_hub import render_tab_body

        return render_tab_body(request, user, db, "po-approval", hub_scope)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/claim", response_class=HTMLResponse)
async def buy_plan_claim_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim an open-pool (RESOURCING) line. First-to-claim wins.

    No per-record ownership gate: the open pool is intentionally claimable by ANY active
    PO-cutter regardless of who owns the parent requisition. The lost race → 409.
    ``origin=approvals_workspace`` re-renders the claimed line's PO pane in place.
    """
    from ....services.buyplan_workflow import claim_line

    _require_po_cutter(user)

    form = await request.form()
    origin = form.get("origin", "")

    try:
        claim_line(plan_id, line_id, user, db)
        db.commit()
    except ValueError as e:
        logger.info("Claim lost/invalid for plan {} line {} by {}: {}", plan_id, line_id, user.id, e)
        raise HTTPException(409, str(e)) from e

    if origin == "approvals_workspace":
        from ..approvals_hub import render_po_pane

        resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/verify-po", response_class=HTMLResponse)
async def buy_plan_verify_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_buyplan_po_approver),
    db: Session = Depends(get_db),
):
    """Ops verifies PO — returns refreshed detail."""
    from ....services.buyplan_notifications import notify_po_rejected, run_notify_bg
    from ....services.buyplan_workflow import verify_po

    form = await request.form()
    action = form.get("action", "approve")
    origin = form.get("origin", "")
    hub_scope = form.get("hub_scope", "all")

    rejection_note = (form.get("rejection_note") or "").strip() or None
    try:
        line = verify_po(plan_id, line_id, action, user, db, rejection_note=rejection_note)
        # verify_po's own internal (approve-only) check_completion call already mutated
        # the SAME identity-mapped BuyPlan object `line.buy_plan` resolves to (verify_po
        # loaded it via db.get(BuyPlan, plan_id) itself) — reading .status off it here
        # is a free identity-map hit, NOT a second completion scan.
        just_completed = line.buy_plan is not None and line.buy_plan.status == BuyPlanStatus.COMPLETED.value
        db.commit()
        if action == "reject":
            await run_notify_bg(notify_po_rejected, plan_id, line_id=line_id)
        await _notify_if_completed(plan_id, just_completed)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e)) from e

    # PO send-back note-to-the-fixer (2.2): the manager's note lands on the LINE's
    # notes thread tagged sent_back; the buyer is emailed via notify_po_rejected
    # above. (The write-only in-app Notification write was deleted, W2.9/§5.5.)
    # The note is optional on a send-back (spec §7).
    if action == "reject" and rejection_note:
        from ....services.workspace_notes import add_note

        add_note(
            db,
            user=user,
            body=rejection_note,
            buy_plan_id=plan_id,
            buy_plan_line_id=line_id,
            decision="sent_back",
        )
        db.commit()

    if origin == "approvals_workspace":
        from ..approvals_hub import render_po_pane

        resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp
    if origin == "approvals_hub":
        from ..approvals_hub import render_tab_body

        return render_tab_body(request, user, db, "po-approval", hub_scope)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/receive", response_class=HTMLResponse)
async def buy_plan_receive_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manually mark a line's goods received (Approvals Workspace 3.2 — the kanban
    Received column's backing action).

    Plain ``require_user`` here — the actor gate (line buyer / manager / admin) and
    the state gate (verified, or the paid-risk prepay state) live service-side in
    ``mark_line_received``; idempotent (an already-received line is a no-op). Never
    touches plan status machinery. ``origin=approvals_workspace`` re-renders the
    workspace pane in place: with ``pane=plan`` the deal pane (the kanban card's Mark
    received), without it the PO-line pane.
    """
    from ....services.buyplan_workflow import mark_line_received

    form = await request.form()
    origin = form.get("origin", "")

    try:
        mark_line_received(plan_id, line_id, user, db)
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if origin == "approvals_workspace":
        from ..approvals_hub import render_plan_pane, render_po_pane

        if form.get("pane") == "plan":
            resp = render_plan_pane(request, user, db, plan_id)
        else:
            resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/issue", response_class=HTMLResponse)
async def buy_plan_flag_issue_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer flags issue on a line — returns refreshed detail."""
    from ....services.buyplan_workflow import flag_line_issue

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    issue_type = form.get("issue_type", "other")
    note = form.get("note", "")

    try:
        flag_line_issue(plan_id, line_id, issue_type, user, db, note=note)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/resolve-issue", response_class=HTMLResponse)
async def buy_plan_resolve_issue_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Supervisor clears a flagged issue → line back to awaiting_po.

    Returns refreshed detail.
    """
    from ....services.buyplan_workflow import resolve_line_issue

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    try:
        resolve_line_issue(plan_id, line_id, user, db)
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)
