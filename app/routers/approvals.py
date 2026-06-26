"""routers/approvals.py — Thin HTMX router for the Approval Engine.

Purpose: HTTP surface for approval workflows. Returns JSON; HTMX partials can
         swap these responses. Delegates all business logic to:
           - app.services.approvals.service (decide)
           - app.services.approvals.events (reassign, cancel)

         Task 12: GET /v2/approvals/requests also merges pending BuyPlan rows
         (read-only bridge). GET /v2/approvals/queue renders the HTML queue
         template surfacing both engine requests and pending buy-plan approvals.

Called by: app.main (router registration).
Depends on: app.services.approvals.service, app.services.approvals.events,
            app.dependencies (require_user, require_approval_gatekeeper),
            app.database (get_db), app.models.approvals, app.models.buy_plan,
            app.constants, app.template_env.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_approval_gatekeeper, require_user
from ..models.approvals import ApprovalRequest
from ..models.auth import User
from ..models.buy_plan import BuyPlan
from ..services.approvals.events import cancel as svc_cancel
from ..services.approvals.events import reassign as svc_reassign
from ..services.approvals.service import decide as svc_decide
from ..template_env import template_response

router = APIRouter(tags=["approvals"])


def _buy_plan_as_queue_item(bp: BuyPlan) -> dict:
    """Serialize a pending BuyPlan as a unified-queue item.

    The source="buy_plan" field distinguishes these read-only bridge items from engine
    ApprovalRequest items. detail_url links to the existing buy-plan detail partial
    (which already has the gated approve/reject actions).
    """
    return {
        "source": "buy_plan",
        "subject_id": bp.id,
        "gate_type": "buy_plan",
        "status": bp.status,
        "amount": str(bp.total_cost) if bp.total_cost is not None else None,
        "currency": "USD",
        "requested_by_id": bp.submitted_by_id,
        "created_at": bp.created_at.isoformat() if bp.created_at else None,
        "detail_url": f"/v2/partials/buy-plans/{bp.id}",
    }


@router.post("/v2/approvals/requests/{id}/decision")
def post_decision(
    id: int,
    action: str = Form(...),
    comment: str | None = Form(default=None),
    db: Session = Depends(get_db),
    acting_user: User = Depends(require_approval_gatekeeper),
):
    """POST a decision (approve/reject) on an ApprovalRequest.

    Gate: require_approval_gatekeeper — 403 unless acting_user is a PENDING recipient.
    Body (form): action ("approve"|"reject"), comment (required for reject).
    Returns: {"id": ..., "status": "approved"|"rejected"}.
    """
    try:
        request = svc_decide(db, id, acting_user, action, comment=comment or None)
        db.commit()
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    return {"id": request.id, "status": request.status}


@router.post("/v2/approvals/requests/{id}/reassign")
def post_reassign(
    id: int,
    to_user_id: int = Form(...),
    db: Session = Depends(get_db),
    acting_user: User = Depends(require_approval_gatekeeper),
):
    """Reassign the acting user's PENDING slot to another user.

    Gate: require_approval_gatekeeper — 403 unless acting_user is a PENDING recipient.
    Body (form): to_user_id (int).
    Returns: {"reassigned": true, "to_user_id": ...}.
    """
    to_user = db.get(User, to_user_id)
    if to_user is None:
        return JSONResponse(status_code=404, content={"error": f"User {to_user_id} not found"})

    try:
        svc_reassign(db, id, acting_user, to_user, actor=acting_user)
        db.commit()
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    return {"reassigned": True, "to_user_id": to_user_id}


@router.post("/v2/approvals/requests/{id}/cancel")
def post_cancel(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Cancel an open ApprovalRequest.

    Gate: require_user (any authenticated user). The service raises ValueError
    if the request is already resolved.
    Returns: {"cancelled": true}.
    """
    try:
        svc_cancel(db, id, actor=current_user)
        db.commit()
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    return {"cancelled": True}


@router.get("/v2/approvals/requests")
def list_requests(
    gate_type: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """List ApprovalRequests merged with pending BuyPlan approvals (read-only bridge).

    Engine items come from ApprovalRequest; buy-plan items are read-only rows
    for BuyPlans with status='pending' (Task 12 bridge). Buy-plan items carry
    source='buy_plan' and detail_url so the caller can link to the existing
    gated approve/reject UI without duplicating that action here.

    Returns: {"items": [...], "total": N}.
    """
    q = select(ApprovalRequest)
    if gate_type and gate_type != "buy_plan":
        q = q.where(ApprovalRequest.gate_type == gate_type)
    elif gate_type:
        # gate_type=buy_plan: engine has no such type; return only buy-plan bridge items
        q = q.where(ApprovalRequest.gate_type == "__never__")
    if status:
        q = q.where(ApprovalRequest.status == status)

    rows = db.execute(q).scalars().all()

    items = [
        {
            "id": r.id,
            "gate_type": r.gate_type,
            "status": r.status,
            "amount": str(r.amount) if r.amount is not None else None,
            "currency": r.currency,
            "requested_by_id": r.requested_by_id,
            "owner_id": r.owner_id,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "resolution_note": r.resolution_note,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]

    # Bridge: merge pending BuyPlan rows unless a non-buy_plan gate_type filter excludes them
    include_buy_plans = not gate_type or gate_type == "buy_plan"
    if include_buy_plans:
        bp_status_filter = status if status else "pending"
        if bp_status_filter == "pending":
            bp_rows = db.execute(select(BuyPlan).where(BuyPlan.status == "pending")).scalars().all()
            items.extend(_buy_plan_as_queue_item(bp) for bp in bp_rows)

    return {"items": items, "total": len(items)}


@router.get("/v2/approvals/queue", response_class=HTMLResponse)
def get_queue(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Render the unified approvals queue as an HTMX partial.

    Surfaces both engine ApprovalRequests and pending BuyPlan approvals in one table.
    Buy-plan rows link to the existing buy-plan detail partial where the gated
    approve/reject actions already live. No behavior change to buy-plan approval — this
    is read-only surfacing only.
    """
    engine_rows = db.execute(select(ApprovalRequest)).scalars().all()
    buy_plan_rows = db.execute(select(BuyPlan).where(BuyPlan.status == "pending")).scalars().all()

    ctx = {
        "request": request,
        "current_user": current_user,
        "engine_requests": engine_rows,
        "buy_plan_rows": buy_plan_rows,
    }
    return template_response("htmx/partials/approvals/_queue.html", ctx)


@router.get("/v2/approvals/requests/{id}")
def get_request(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Get a single ApprovalRequest by id.

    Returns: the request object as a dict.
    Raises: 404 if not found.
    """
    request = db.get(ApprovalRequest, id)
    if request is None:
        return JSONResponse(status_code=404, content={"error": f"ApprovalRequest {id} not found"})

    return {
        "id": request.id,
        "gate_type": request.gate_type,
        "status": request.status,
        "amount": str(request.amount) if request.amount is not None else None,
        "currency": request.currency,
        "requested_by_id": request.requested_by_id,
        "owner_id": request.owner_id,
        "resolved_at": request.resolved_at.isoformat() if request.resolved_at else None,
        "resolution_note": request.resolution_note,
        "created_at": request.created_at.isoformat() if request.created_at else None,
    }
