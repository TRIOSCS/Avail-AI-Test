"""
routers/htmx/buy_plans.py — HTMX partials for buy plan workflow.

Handles list, detail, submit, approve, verify-SO, confirm-PO, verify-PO,
flag-issue, cancel, and reset actions for buy plans.

Called by: htmx __init__.py (router included via shared router object)
Depends on: _helpers (router, templates, _base_ctx, escape_like),
            models (BuyPlan, BuyPlanLine, Quote, CustomerSite, User, etc.),
            services.buyplan_workflow, services.buyplan_notifications
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload, selectinload

from ...database import get_db
from ...dependencies import require_user
from ...models import (
    BuyPlan,
    BuyPlanLine,
    CustomerSite,
    Quote,
    User,
    VerificationGroupMember,
)
from ...models.buy_plan import BuyPlanStatus
from ._helpers import _base_ctx, escape_like, router, templates


def _is_ops_member(user: User, db: Session) -> bool:
    """Check if user is in the ops verification group."""
    return db.query(VerificationGroupMember).filter_by(user_id=user.id, is_active=True).first() is not None


@router.get("/partials/buy-plans", response_class=HTMLResponse)
async def buy_plans_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    mine: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plans list as HTML partial."""
    query = db.query(BuyPlan).options(
        joinedload(BuyPlan.quote).joinedload(Quote.customer_site).joinedload(CustomerSite.company),
        joinedload(BuyPlan.requisition),
        joinedload(BuyPlan.submitted_by),
        joinedload(BuyPlan.approved_by),
        selectinload(BuyPlan.lines),
    )

    if status:
        query = query.filter(BuyPlan.status == status)
    if mine:
        query = query.filter(BuyPlan.submitted_by_id == user.id)
    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(
            BuyPlan.sales_order_number.ilike(f"%{safe}%") | BuyPlan.customer_po_number.ilike(f"%{safe}%")
        )

    # Sales users only see their own
    if user.role == "sales":
        query = query.filter(BuyPlan.submitted_by_id == user.id)

    plans = query.order_by(BuyPlan.created_at.desc()).limit(200).all()

    # Build lightweight list items
    buy_plans = []
    for p in plans:
        customer_name = None
        if p.quote and p.quote.customer_site:
            site = p.quote.customer_site
            co = site.company if hasattr(site, "company") else None
            customer_name = co.name if co else getattr(site, "site_name", None)

        buy_plans.append(
            {
                "id": p.id,
                "quote_id": p.quote_id,
                "quote_number": p.quote.quote_number if p.quote else None,
                "customer_name": customer_name,
                "sales_order_number": p.sales_order_number,
                "status": p.status,
                "so_status": p.so_status,
                "total_cost": float(p.total_cost) if p.total_cost else 0,
                "total_margin_pct": float(p.total_margin_pct) if p.total_margin_pct else 0,
                "line_count": len(p.lines) if p.lines else 0,
                "submitted_by_name": p.submitted_by.name if p.submitted_by else None,
                "auto_approved": p.auto_approved or False,
                "created_at": str(p.created_at) if p.created_at else None,
            }
        )

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "buy_plans": buy_plans,
            "q": q,
            "status": status,
            "mine": mine,
            "total": len(buy_plans),
        }
    )
    return templates.TemplateResponse("htmx/partials/buy_plans/list.html", ctx)


@router.get("/partials/buy-plans/{plan_id}", response_class=HTMLResponse)
async def buy_plan_detail_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plan detail as HTML partial."""
    bp = (
        db.query(BuyPlan)
        .options(
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.buyer),
            joinedload(BuyPlan.quote),
            joinedload(BuyPlan.requisition),
            joinedload(BuyPlan.submitted_by),
            joinedload(BuyPlan.approved_by),
        )
        .filter(BuyPlan.id == plan_id)
        .first()
    )
    if not bp:
        raise HTTPException(404, "Buy plan not found")

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "bp": bp,
            "lines": bp.lines or [],
            "is_ops_member": _is_ops_member(user, db),
            "user": user,
        }
    )
    return templates.TemplateResponse("htmx/partials/buy_plans/detail.html", ctx)


@router.post("/partials/buy-plans/{plan_id}/submit", response_class=HTMLResponse)
async def buy_plan_submit_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a draft buy plan with SO# — returns refreshed detail partial."""
    from ...services.buyplan_notifications import (
        notify_approved,
        notify_submitted,
        run_notify_bg,
    )
    from ...services.buyplan_workflow import submit_buy_plan

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
        if plan.auto_approved:
            run_notify_bg(notify_approved, plan.id)
        else:
            run_notify_bg(notify_submitted, plan.id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/partials/buy-plans/{plan_id}/approve", response_class=HTMLResponse)
async def buy_plan_approve_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager approves or rejects a pending buy plan — returns refreshed detail."""
    from ...services.buyplan_notifications import (
        notify_approved,
        notify_rejected,
        run_notify_bg,
    )
    from ...services.buyplan_workflow import approve_buy_plan

    form = await request.form()
    action = form.get("action", "approve")

    if user.role not in ("manager", "admin"):
        raise HTTPException(403, "Manager or admin role required")

    try:
        plan = approve_buy_plan(plan_id, action, user, db, notes=form.get("notes"))
        db.commit()
        if action == "approve":
            run_notify_bg(notify_approved, plan.id)
        else:
            run_notify_bg(notify_rejected, plan.id)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/partials/buy-plans/{plan_id}/verify-so", response_class=HTMLResponse)
async def buy_plan_verify_so_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies SO — returns refreshed detail."""
    from ...services.buyplan_notifications import (
        notify_so_rejected,
        notify_so_verified,
        run_notify_bg,
    )
    from ...services.buyplan_workflow import verify_so

    form = await request.form()
    action = form.get("action", "approve")

    try:
        plan = verify_so(
            plan_id,
            action,
            user,
            db,
            rejection_note=form.get("rejection_note"),
        )
        db.commit()
        if action == "approve":
            run_notify_bg(notify_so_verified, plan.id)
        else:
            run_notify_bg(notify_so_rejected, plan.id, action=action)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/partials/buy-plans/{plan_id}/lines/{line_id}/confirm-po", response_class=HTMLResponse)
async def buy_plan_confirm_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer confirms PO — returns refreshed detail."""
    from datetime import datetime

    from ...services.buyplan_notifications import notify_po_confirmed, run_notify_bg
    from ...services.buyplan_workflow import confirm_po

    form = await request.form()
    po_number = form.get("po_number", "").strip()
    ship_date_str = form.get("estimated_ship_date", "")

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

    try:
        confirm_po(plan_id, line_id, po_number, ship_date, user, db)
        db.commit()
        run_notify_bg(notify_po_confirmed, plan_id, line_id=line_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/partials/buy-plans/{plan_id}/lines/{line_id}/verify-po", response_class=HTMLResponse)
async def buy_plan_verify_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies PO — returns refreshed detail."""
    from ...services.buyplan_notifications import notify_completed, run_notify_bg
    from ...services.buyplan_workflow import check_completion, verify_po

    form = await request.form()
    action = form.get("action", "approve")

    try:
        verify_po(plan_id, line_id, action, user, db, rejection_note=form.get("rejection_note"))
        db.commit()
        updated = check_completion(plan_id, db)
        if updated and updated.status == "completed":
            db.commit()
            run_notify_bg(notify_completed, plan_id)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/partials/buy-plans/{plan_id}/lines/{line_id}/issue", response_class=HTMLResponse)
async def buy_plan_flag_issue_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer flags issue on a line — returns refreshed detail."""
    from ...services.buyplan_workflow import flag_line_issue

    form = await request.form()
    issue_type = form.get("issue_type", "other")
    note = form.get("note", "")

    try:
        flag_line_issue(plan_id, line_id, issue_type, user, db, note=note)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/partials/buy-plans/{plan_id}/cancel", response_class=HTMLResponse)
async def buy_plan_cancel_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cancel a buy plan — returns refreshed detail."""
    from datetime import datetime, timezone

    bp = db.get(BuyPlan, plan_id)
    if not bp:
        raise HTTPException(404, "Buy plan not found")
    if bp.status in ("completed", "cancelled"):
        raise HTTPException(400, f"Cannot cancel plan in '{bp.status}' status")

    form = await request.form()
    bp.status = BuyPlanStatus.cancelled.value
    bp.cancelled_at = datetime.now(timezone.utc)
    bp.cancelled_by_id = user.id
    bp.cancellation_reason = form.get("reason")
    db.commit()

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/partials/buy-plans/{plan_id}/reset", response_class=HTMLResponse)
async def buy_plan_reset_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reset halted/cancelled plan to draft — returns refreshed detail."""
    from ...services.buyplan_workflow import reset_buy_plan_to_draft

    try:
        reset_buy_plan_to_draft(plan_id, user, db)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)
